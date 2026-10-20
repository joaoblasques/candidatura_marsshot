"""Email & command parsing (U3).

Two jobs, both best-effort over a *forwarded* message:

1. Detect the CEO's command (a `WIYOW`/`S` code on its own line in the authored
   region above the first quoted block — KTD8 leading-token rule — otherwise a
   natural-language instruction).
2. Recover the forwarded original thread (sender, recipients, subject) so the
   draft can be addressed. Forwarded headers are rendered into the body in
   client-specific formats (Gmail / Apple Mail / Outlook), not MIME headers, so
   this is heuristic. When the reply recipient cannot be confidently determined,
   ``recipient_uncertain`` is set so U5/U6 surface it via the uncertainty flag
   (R11) rather than asserting a recipient.
"""

from __future__ import annotations

import re

from app.attachments.extract import extract_attachments
from app.codes import PREFIX, CodeParse, parse_code_token
from app.drafting.voice import extract_ceo_exemplar
from app.email.markers import EMAIL_RE as _EMAIL_RE
from app.email.markers import ON_WROTE_RE as _ON_WROTE_RE
from app.email.markers import QUOTE_MARKERS as _QUOTE_MARKERS
from app.models import Attachment, Command, InboundEmail, ParsedRequest, ParsedThread

_BRACKET_RE = re.compile(r"\[([^\]]*)\]")
# Fail-closed keep-local detection: the KL safety code must be honored wherever it
# appears as a standalone token in the CEO-authored region, not only in the strict
# leading code-token position. \bKL\b (case-insensitive) matches "KL" / "kl" as a whole
# token but not inside words ("weekly", "KLM"). Scanned over the authored region only.
_KEEP_LOCAL_RE = re.compile(r"\bKL\b", re.IGNORECASE)


def _legacy_command(action: str | None) -> Command:
    """Map a parsed primary action to the legacy Command enum (backward compat)."""
    if action in ("RE", "WIYOW"):
        return Command.WIYOW
    if action in ("SC", "S"):
        return Command.S
    return Command.NONE

def split_authored_region(body: str) -> tuple[str, str]:
    """Split the body into (CEO-authored text, quoted/forwarded remainder).

    The boundary is the first of: a forwarded/original-message marker, an
    "On … wrote:" attribution, a ``>``-quoted line, or a run of header lines
    (Outlook-style). Conservative: if no boundary is found the whole body is
    treated as authored.
    """
    lines = body.splitlines()
    for i, line in enumerate(lines):
        stripped = line.strip().lower()
        is_boundary = (
            any(stripped.startswith(m) for m in _QUOTE_MARKERS)
            or _ON_WROTE_RE.match(line)
            or line.lstrip().startswith(">")
            # Outlook: a "From:" header begins the forwarded block — but only a REAL
            # header, not authored prose like "From: my read of this, ...". Treating a
            # prose "From:" as the boundary would truncate the authored region and drop
            # a later code (e.g. KL) — a fail-open. See _is_forward_header.
            or _is_forward_header(lines, i)
        )
        if is_boundary:
            return "\n".join(lines[:i]), "\n".join(lines[i:])
    return body, ""


_FORWARD_SIBLING_HEADERS = ("to:", "cc:", "bcc:", "sent:", "date:", "subject:", "reply-to:")


def _is_forward_header(lines: list[str], i: int) -> bool:
    """True only for a genuine forwarded ``From:`` header — one that carries an email
    address, or is immediately followed (within 3 lines) by a sibling header. A prose
    line that merely starts with "From:" is NOT a boundary (so a following code survives)."""
    line = lines[i].strip()
    if not line.lower().startswith("from:"):
        return False
    if _EMAIL_RE.search(line):
        return True
    for j in range(i + 1, min(i + 4, len(lines))):
        if lines[j].strip().lower().startswith(_FORWARD_SIBLING_HEADERS):
            return True
    return False


def parse_command_line(authored: str) -> tuple[CodeParse, str | None]:
    """Parse the authored region for a leading NB code sequence + instruction/notes.

    A line is treated as codes only when its first token is NB-prefixed, or the
    whole first line (minus ``[bracketed notes]``) is a single recognized code
    token. This keeps natural-language instructions (and an incidental code word
    mid-sentence) from misrouting — matching the conservative leading-token rule.
    Returns (CodeParse, instruction_text); for natural language the CodeParse is
    empty and instruction_text holds the request.
    """
    nonempty = [ln.strip() for ln in authored.splitlines() if ln.strip()]
    if not nonempty:
        return CodeParse(), None
    first, rest_lines = nonempty[0], nonempty[1:]
    # Keep each [bracket] note on its own line (not space-joined) so distinct notes don't bleed
    # together — e.g. a `[remember: …]` relationship directive must not absorb a sibling `[warmly]`.
    notes = "\n".join(m.strip() for m in _BRACKET_RE.findall(first) if m.strip())
    first_wo_notes = _BRACKET_RE.sub(" ", first).strip()
    tokens = first_wo_notes.split()
    is_code_line = bool(tokens) and (
        tokens[0].upper().startswith(PREFIX)
        or (len(tokens) == 1 and parse_code_token(tokens[0]).recognized)
    )
    if is_code_line:
        cp = parse_code_token(tokens[0])
        trailing = " ".join(tokens[1:]).strip()
        instruction = " ".join(p for p in (notes, trailing, *rest_lines) if p).strip() or None
    else:
        cp = CodeParse()
        instruction = " ".join(nonempty).strip() or None
    # Fail-closed keep-local: honor KL wherever it appears as a standalone token in the
    # authored region — including natural-language ("reply to this KL") and space-separated
    # ("NB-RE KL") forms the strict leading-token parser would otherwise silently drop,
    # leaking sensitive content to the cloud. Authored region only (never the quoted
    # thread), so forwarded content can't inject it. A false positive routes to the local
    # model (safe over-protection); a false negative would leak — so we bias to detect.
    if "KL" not in cp.modifiers and _KEEP_LOCAL_RE.search(authored):
        cp.modifiers.append("KL")
        cp.recognized = True
    return cp, instruction


def detect_command(authored: str) -> tuple[Command, str | None]:
    """Backward-compatible wrapper: legacy (Command, instruction) view of the codes."""
    cp, instruction = parse_command_line(authored)
    return _legacy_command(cp.action), instruction


def _header_emails(label: str, text: str, *, first_only: bool) -> list[str]:
    """Emails from header lines matching ``label`` (e.g. 'to').

    ``first_only`` stops at the first matching line (the common case); otherwise
    it collects across every matching line (used to detect multi-sender chains).
    """
    found: list[str] = []
    for line in text.splitlines():
        if re.match(rf"^\s*{label}\s*:", line, re.IGNORECASE):
            found.extend(_EMAIL_RE.findall(line))
            if first_only:
                break
    return found


def parse_forwarded_thread(quoted: str) -> ParsedThread:
    """Extract sender, recipients, and subject from the forwarded header block."""
    senders = _header_emails("from", quoted, first_only=True)
    recipients = _header_emails("to", quoted, first_only=True) + _header_emails(
        "cc", quoted, first_only=True
    )
    subject_match = re.search(r"^\s*subject\s*:\s*(.+)$", quoted, re.IGNORECASE | re.MULTILINE)
    subject = subject_match.group(1).strip() if subject_match else None
    return ParsedThread(
        original_sender=senders[0] if senders else None,
        recipients=recipients,
        subject=subject,
        body=quoted,
    )


def recover_recipient(thread: ParsedThread, quoted: str) -> tuple[str | None, bool]:
    """Determine the intended reply recipient; flag uncertainty.

    The reply target is normally the original sender. It is uncertain when no
    sender was recovered, or when the thread shows multiple distinct senders
    (a multi-party / reply-all chain) where the right target is ambiguous.
    """
    all_senders = {e.lower() for e in _header_emails("from", quoted, first_only=False)}
    if not thread.original_sender:
        return None, True
    if len(all_senders) > 1:
        return thread.original_sender, True
    return thread.original_sender, False


def parse_request(email: InboundEmail) -> ParsedRequest:
    """Top-level: produce a ParsedRequest from a normalized InboundEmail."""
    authored, quoted = split_authored_region(email.body)
    cp, instruction = parse_command_line(authored)
    thread = parse_forwarded_thread(quoted)
    # Store metadata only on the (persisted) thread — never the raw bytes — and
    # attach the locally-extracted text alongside.
    thread.attachments = [
        Attachment(filename=a.filename, content_type=a.content_type, size=a.size)
        for a in email.attachments
    ]
    thread.attachment_extracts = extract_attachments(email.attachments)
    recipient, uncertain = recover_recipient(thread, quoted)
    if recipient and not thread.original_sender:
        thread.original_sender = recipient
    # Voice exemplar (U1): the CEO's own prior messages in this thread, keyed on the
    # DKIM-verified forwarder address only (never in-thread name attribution — KTD-A).
    # Multi-alias matching is deferred (parse_request isn't passed the registry today).
    exemplar = extract_ceo_exemplar(thread.body, {email.from_address}) if email.from_address else ""
    return ParsedRequest(
        command=_legacy_command(cp.action),
        instruction_text=instruction,
        thread=thread,
        recipient_uncertain=uncertain,
        correlation_token=email.correlation_token,
        action_code=cp.action,
        deferred_code=cp.deferred_action,
        modifier_codes=cp.modifiers,
        target_codes=cp.targets,
        raw_code_line=(cp.raw or None) if cp.recognized else None,
        voice_exemplar=exemplar,
        approval_reaction=cp.approval,
    )
