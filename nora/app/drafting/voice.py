"""In-thread voice exemplar extraction (voice-fidelity drafting).

A forwarded thread frequently already contains the CEO's own earlier replies — a
latent, zero-infrastructure voice exemplar. This module segments the quoted
thread body by author and returns only the messages attributed to the *verified*
CEO address(es) as a token-capped style sample, or "" when none are found.

SECURITY — read carefully (KTD-A, corrected). In-thread attribution lines
("On … <addr> wrote:" / "From: …") live in the *untrusted* forwarded body. DKIM
verifies who *forwarded* the mail (the wrapper From), NOT who authored each quoted
line. So matching the verified CEO address against an in-thread attribution is
**best-effort, not proof of authorship** — a thread participant who knows the
CEO's address can forge a "CEO wrote:" line. This extraction therefore is NOT a
trust boundary. The real, load-bearing defenses are: (1) drafts-only — the CEO
reviews every draft before it can be sent; (2) the exemplar is framed to the model
as STYLE-ONLY data inside a sanitized data fence, so embedded instructions don't
execute and the samples contribute no facts/recipients; (3) the never-impersonate
recipient guard (KTD9) is unchanged. As surface reduction we require the CEO
attribution to sit at quote-depth 0 (the most-recent exchange), which drops the
common nested-forgery pattern and deeply-nested low-value samples; it does not and
cannot make in-thread attribution tamper-proof.

This builds a NEW author-segmentation loop; ``app/email/parser.py``'s
``split_authored_region`` is a one-shot top/bottom splitter and is reused here only
for its marker *patterns*, not its return shape.
"""

from __future__ import annotations

import re

from app.email.markers import EMAIL_RE, ON_WROTE_RE, QUOTE_MARKERS
from app.logging import get_logger

logger = get_logger(__name__)

# Tolerates leading whitespace before the quote run so indented quotes de-quote too.
_DEQUOTE_RE = re.compile(r"^\s*(>\s?)+")
# Header lines inside a forwarded block that are noise, not the author's prose.
_HEADER_PREFIXES = ("to:", "subject:", "sent:", "cc:", "bcc:", "date:", "reply-to:")


def _dequote(line: str) -> str:
    """Strip leading ``>`` quote markers (one or more levels) — recover the text."""
    return _DEQUOTE_RE.sub("", line)


def _quote_depth(line: str) -> int:
    """The quote nesting depth of a line (number of leading ``>`` markers)."""
    m = _DEQUOTE_RE.match(line)
    return m.group(0).count(">") if m else 0


def _attribution_author(dequoted: str) -> str | None:
    """If the de-quoted line is an attribution ("On … wrote:" / "From: …"), return
    its email address (or "" when the attribution carries no parseable address);
    otherwise None. An empty-string author still ends the prior segment — a new
    author's text must not bleed into the previous one — but is later dropped."""
    lowered = dequoted.strip().lower()
    if ON_WROTE_RE.match(dequoted) or lowered.startswith("from:"):
        m = EMAIL_RE.search(dequoted)
        return m.group(0).lower() if m else ""
    return None


def _is_noise(dequoted: str) -> bool:
    """Forwarded-block header lines and forward markers are not authored prose."""
    lowered = dequoted.strip().lower()
    return lowered.startswith(_HEADER_PREFIXES) or any(
        lowered.startswith(m) for m in QUOTE_MARKERS
    )


def _clean_segment(lines: list[str]) -> str:
    """Drop a trailing signature block and trim surrounding/excess blank lines."""
    out: list[str] = []
    for raw in lines:
        if raw.strip() == "--":  # standard signature delimiter → stop
            break
        out.append(raw)
    text = "\n".join(out).strip("\n")
    # collapse 3+ blank lines to a single blank line
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _segments_by_author(thread_body: str) -> list[tuple[str, int, str]]:
    """Walk the thread, returning (author_email, attribution_depth, cleaned_text) in
    document order (top = newest). ``attribution_depth`` is the quote depth of the
    segment's attribution line; the caller uses it to drop deeply-nested (likely
    forged or low-value) segments. A deeper-nested attribution inside a block
    naturally starts a new segment with its own author."""
    result: list[tuple[str, int, str]] = []
    current_author: str | None = None
    current_depth = 0
    buffer: list[str] = []

    def _flush() -> None:
        if current_author is not None:
            text = _clean_segment(buffer)
            if current_author and text:
                result.append((current_author, current_depth, text))

    for line in thread_body.splitlines():
        dequoted = _dequote(line)
        author = _attribution_author(dequoted)
        if author is not None:
            _flush()
            current_author = author
            current_depth = _quote_depth(line)
            buffer = []
            continue
        if current_author is None:
            continue  # unattributed preamble — can't confirm authorship, skip
        if _is_noise(dequoted):
            continue
        buffer.append(dequoted)
    _flush()
    return result


def _truncate_words(text: str, limit: int) -> str:
    """Truncate to ``limit`` chars on a word boundary (best-effort)."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    sp = cut.rfind(" ")
    return (cut[:sp] if sp > 0 else cut).rstrip()


def extract_ceo_exemplar(
    thread_body: str,
    ceo_addresses: set[str] | list[str] | tuple[str, ...],
    *,
    char_budget: int = 1500,
    max_segments: int = 3,
) -> str:
    """Return a token-capped style exemplar of the CEO's own prior messages in the
    forwarded thread, or "" when none are confidently found.

    ``ceo_addresses`` are the verified CEO address(es); matching is exact-email,
    case-insensitive — never display-name (KTD-A). Most-recent (top) first, capped
    to ``max_segments`` messages and ``char_budget`` characters total. Never raises.
    """
    try:
        if not thread_body or not thread_body.strip():
            return ""
        allowed = {a.strip().lower() for a in ceo_addresses if a and a.strip()}
        if not allowed:
            return ""

        # Require attribution at quote-depth 0 (most-recent exchange). This is
        # surface reduction, not a trust boundary (see module docstring): it drops
        # the common nested-forgery pattern and deeply-nested low-value samples.
        ceo_texts = [
            text
            for author, depth, text in _segments_by_author(thread_body)
            if author in allowed and depth == 0
        ]
        if not ceo_texts:
            return ""

        # Most-recent (top) first, capped to max_segments, then to char_budget total.
        joined = "\n\n".join(ceo_texts[:max_segments])
        return _truncate_words(joined, char_budget)
    except Exception:  # never let exemplar extraction break drafting
        # Fail-soft to generic, but make a systemic regression observable (no content).
        logger.warning("voice exemplar extraction failed; falling back to generic", exc_info=True)
        return ""
