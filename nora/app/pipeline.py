"""Request pipeline orchestration (ties U2–U7 together).

Given an authenticated InboundEmail, produce the CEO-facing reply and the
plus-addressed reply-to that carries the revise correlation token. Handles three
inbound shapes: a fresh request (parse → route → draft/summarize), a correlated
revise reply (re-draft from stored thread), and a reply whose correlation was
lost (graceful notice).
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.calendar import CalendarClient, NullCalendarClient, parse_slot
from app.codes import DEFERRED_LABEL, HELP_TEXT
from app.config import Settings
from app.drafting.claude import DraftingError, DraftingService
from app.drafting.result import Drafting
from app.email.inbound import build_secret_address
from app.email.parser import (
    parse_command_line,
    parse_forwarded_thread,
    parse_request,
    split_authored_region,
)
from app.filesource import FileSource, NullFileSource
from app.interpreter import route
from app.memory import MemoryStore, NullMemoryStore
from app.onboarding import access_request_reply, provision_signup, signup_eligible, welcome_reply
from app.nl_interpreter import Interpreter, NullInterpreter, apply_interpretation
from app.relationship import parse_relationship_directive
from app.triage import parse_triage_followup, relationship_hint, split_threads
from app.users import NullUserStore, UserStore
from app.voice_memory import NullVoiceStore, VoiceStore
from app.metrics import emit
from app.models import (
    Command,
    InboundEmail,
    Intent,
    NoraResult,
    ParsedRequest,
    ParsedThread,
    ResultKind,
)
from app.posture import Rung, resolve_posture
from app.receipt import ComposedReply, compose_reply
from app.reports.render import SUPPORTED_REPORT_FORMATS, render_report
from app.research import NullResearchClient, ResearchClient, ResearchUnavailable
from app.tenant import NullTenantDraftClient, TenantDraftClient, TenantUnavailable
from app.state.store import (
    InMemoryPendingActionStore,
    InMemoryTriageStore,
    PendingActionRecord,
    PendingActionStore,
    ReviseRecord,
    StateStore,
    TriageSessionRecord,
    TriageStore,
    new_correlation_token,
)

logger = logging.getLogger(__name__)

# The parser strips the [brackets] before this runs, so we can't rely on a ']' to
# terminate the name (a trailing note would otherwise leak into the filename). Prefer a
# quoted name; else stop at a filename extension; else take a single bare token.
_FILE_REF_RE = re.compile(
    r"""\bfile:\s*(?:
        '([^']+)'
      | "([^"]+)"
      | (\S+(?:\s\S+)*?\.[A-Za-z0-9]{1,8})\b
      | (\S+)
    )""",
    re.IGNORECASE | re.VERBOSE,
)


def _relationship_context(mem) -> str | None:
    """Render a cross-thread memory breadcrumb into the drafting prompt's relationship_context
    (R1). Curated facts read as remembered; auto-derived (Layer C v2) are framed UNCONFIRMED so
    the model treats them as hints, never asserts them, never surfaces them to the recipient.
    Shared by the forward path (_handle_draft) and the ambient auto-draft path."""
    if not mem or not (mem.thread_count > 0 or mem.notes or mem.derived):
        return None
    ctx = f"prior exchanges with this counterparty: {mem.thread_count}; last {mem.last_seen}"
    if mem.notes:
        ctx += "; remembered: " + "; ".join(mem.notes)
    if mem.derived:
        ctx += (
            "; observed across past threads (UNCONFIRMED — use only if helpful, do "
            "not assert as fact or surface to the recipient): " + "; ".join(mem.derived)
        )
    return ctx


def _file_ref(instruction: str | None) -> str | None:
    """Extract a file reference from an instruction like 'file: Q3-deck.pdf'. Robust to
    trailing notes/prose after the reference (the [brackets] are already gone here)."""
    if not instruction:
        return None
    m = _FILE_REF_RE.search(instruction)
    if not m:
        return None
    ref = next((g for g in m.groups() if g), "")
    return ref.strip().strip("'\"") or None


# "the file of X" (X runs to a clause/end) takes priority over "the X file" so the literal
# word "file" in "file of" can't be mistaken for the trailing-"file" terminator.
_NL_FILE_OF_RE = re.compile(r"\bfile\s+of\s+([^\n,.;]+?)\s*(?:,|\.|;|\band\b|$)", re.IGNORECASE)
_NL_THE_X_FILE_RE = re.compile(
    r"\b(?:get|find|pull(?:\s+up)?|fetch|send|grab|read|summari[sz]e|open|review|check"
    r"|retrieve|obtain|locate|access|bring|show|give)\b"
    r"[^\n.,;]*?\bthe\s+([^\n.,;]+?)\s+file\b",
    re.IGNORECASE,
)


def _nl_file_descriptor(text: str | None) -> str | None:
    """Heuristic: extract a file descriptor from a plain-language fetch request
    ('get me the Q4 earnings file', 'the file of X'). Returns None when no fetch intent —
    used as a fallback when no explicit 'file: NAME' reference is present."""
    if not text:
        return None
    for pattern in (_NL_FILE_OF_RE, _NL_THE_X_FILE_RE):
        m = pattern.search(text)
        if m:
            return m.group(1).strip() or None
    return None


def _resolve_referenced_file(source: FileSource, ref: str) -> tuple[list[str], str | None]:
    """Resolve a (fuzzy) file reference. Returns ``(candidates, file_context)`` where
    ``candidates`` is every name matching ``ref`` and ``file_context`` is the labelled text
    of the SINGLE match — or None for zero or multiple matches (multiple → the caller
    disambiguates rather than guessing, per the 'no wrong file' product line)."""
    candidates = source.search(ref)
    if len(candidates) != 1:
        return candidates, None
    text = source.read(candidates[0])
    return candidates, (f"{candidates[0]}\n{text}" if text else None)


def _read_referenced_file(source: FileSource, ref: str) -> str | None:
    """Read a referenced file into fenced draft context. For access (read) we don't block
    on ambiguity (unlike share): use the best match but flag any ambiguity so the model
    surfaces it. Returns the labelled file text, or None when nothing matched/read."""
    candidates = source.search(ref)
    if not candidates:
        return None
    text = source.read(candidates[0])
    if not text:
        return None
    note = (
        ""
        if len(candidates) == 1
        else f" [note: '{ref}' matched several files; used {candidates[0]}]"
    )
    return f"{candidates[0]}{note}\n{text}"


def _attach_report_file(
    reply: ComposedReply, result: NoraResult, intent: Intent, modifier_codes: list[str] | None
) -> None:
    """If a REPORT requested a file format (PR.W/.X/.PPT), render it from the report
    text and attach it to the reply. The inline report stays in the body; the file is
    a convenience copy. Best-effort: a render failure leaves the email-text report intact."""
    if intent is not Intent.REPORT or result.kind is not ResultKind.REPORT:
        return
    fmt = next((m for m in (modifier_codes or []) if m in SUPPORTED_REPORT_FORMATS), None)
    if not fmt:
        return
    try:
        reply.attachments.append(render_report(result.text, fmt))
    except Exception:  # noqa: BLE001 — never fail the reply over a file render
        logger.exception("report file render failed (fmt=%s)", fmt)


@dataclass
class ActionDescriptor:
    """A consent-confirmed action for app/main.py to execute (the third-party send).
    Produced only by the confirm path AFTER the pending record is consumed (KTD-D);
    main owns the actual I/O so the pipeline stays decide-only."""

    recipient: str
    subject: str
    body: str
    token: str
    action_kind: str = "send_email"  # mirrors PendingActionRecord.action_kind
    payload: dict = field(default_factory=dict)  # kind-specific extra data


@dataclass
class _HandlerResult:
    """What a handler produced: the reply (None when the outcome receipt is composed
    post-send by main), the optional correlation reply-to, and the result kind."""

    reply: ComposedReply | None
    reply_to: str | None
    result_kind: ResultKind
    # An action for main to execute after the CEO receipt path (confirm → send).
    action: ActionDescriptor | None = None
    # Non-sensitive metric signals (U3); consumed by handle() for one draft event.
    action_code: str | None = None
    deferred_code: str | None = None
    nl_fallback: bool = False
    revision_count: int = 0
    recipient_uncertain: bool = False
    rung: str = "R1"


_FIRST_CONTACT_NOTICE = (
    "\n\n— First time using Nora: in this version the email content you forward is "
    "processed by an AI service (Claude) to prepare this. Nora never sends on your "
    "behalf — you always send."
)

# Local-path variant: the draft was produced by a LOCAL on-device model — content never
# reached any external AI service. Disclosing the external-LLM notice here would be false
# (and would contradict the guarantee), so the local path gets its own accurate disclosure.
_FIRST_CONTACT_NOTICE_LOCAL = (
    "\n\n— First time using Nora for higher-assurance (local-only) handling: the email "
    "content you forward this way is processed by a local AI model on Nora's own "
    "infrastructure — it never leaves to an external AI service. Nora never sends on your "
    "behalf — you always send."
)

# Per-email receipt appended to every KL ("keep local") draft so the CEO has a standing
# confirmation that this specific message stayed on-device.
_KEPT_LOCAL_RECEIPT = (
    "\n\n— Kept local (KL): I drafted this on Nora's on-device model; the content never "
    "left the sandbox and was not sent to the external Cloud AI service."
)

# Shown when content needs higher-assurance (local-only) handling that cannot be served in
# this version (no local backend, or a future rung). The content was NOT processed and
# nothing was sent to any AI service. Wording is neutral so it fits both the per-email KL
# code and a per-relationship posture.
_HIGHER_ASSURANCE_TEXT = (
    "This message needs higher-assurance (local-only) handling that isn't available in this "
    "version — I haven't processed it, and nothing was sent to the AI service."
)

_TRIAGE_KL_DECLINE = (
    "Triage runs on the Cloud model, so it can't be combined with KL (keep-local) yet. "
    "Forward keep-local threads one at a time with KL, and I'll keep each on-device."
)


@dataclass
class Pipeline:
    settings: Settings
    drafting: DraftingService
    store: StateStore
    # Pending consent-gated actions, awaiting CEO confirmation (0.2.0). A SEPARATE
    # store instance from `store` so pending-action and revise tokens never cross-route
    # (KTD-B). Defaults to a fresh in-memory store so existing constructions still work.
    actions: PendingActionStore | None = None
    # In-memory set of "<sender>|<external|local>" keys already shown the first-contact
    # disclosure (KTD5) — keyed per processing-kind so the external-LLM and the local
    # in-boundary (R3) disclosures are each shown once. In-memory single instance (U8).
    notified: set[str] = field(default_factory=set)
    # Pluggable web-research backend (FI/I). Defaults to NullResearchClient (default-off):
    # FI/I decline gracefully until a real provider is wired. ResearchClient type annotation
    # is structural (Protocol), so any object with research(query)->str qualifies.
    research: ResearchClient | None = None
    # Pluggable in-tenant draft backend (R2). Defaults to NullTenantDraftClient (default-off):
    # R2 relationships decline without any LLM call until a real tenant backend is wired.
    # CRITICAL: default-Null R2 must NOT call self.drafting (no LLM on undeclined R2 content).
    tenant: TenantDraftClient | None = None
    # Pluggable LOCAL (in-boundary) drafting backend for R3 relationships. Default None =>
    # R3 declines (no LLM call at all). When set (e.g. an OllamaDraftingService), R3 producing
    # intents draft on local hardware — content never reaches the external Claude API. This is
    # SEPARATE from `drafting` (the external Claude backend used for R1): R1-Claude and R3-local
    # coexist in one process, so the global drafting_backend / boot guard are unaffected.
    local_drafting: Drafting | None = None
    # Cross-thread relationship memory (Layer A). Defaults to NullMemoryStore (off): no
    # recall, no record. Only ever used on the R1 path (KL/R3/R2 return before drafting).
    memory: MemoryStore | None = None
    # Layer B voice memory. Defaults to NullVoiceStore (off). R1-only, coded signals only.
    voice: VoiceStore | None = None
    # R1 self-serve onboarding: dynamic user registry. Defaults to NullUserStore (off).
    users: UserStore | None = None
    # TR v2: triage-session store (items behind a digest), so a reply '2 RE' can act per-item.
    triage_sessions: TriageStore | None = None
    # Plain-language interpreter (NL front-end). Defaults to NullInterpreter (off = keyword fallback).
    interpreter: Interpreter | None = None
    # Files-domain access (Layer A). Defaults to NullFileSource (off): a referenced file
    # resolves to nothing. Read-only; no consent gate (principal's own file → their draft).
    file_source: FileSource | None = None
    # R3 in-boundary file source (LocalFileVault). SEPARATE from file_source: the R3 path
    # reads only from here (never the Cloud Drive/OneDrive source), so content never egresses.
    local_file_source: FileSource | None = None
    # Calendar client — used READ-ONLY here (propose-time conflict heads-up); the actual
    # consent-gated booking runs in main.py at confirm time. Default Null ⇒ no heads-up.
    calendar: CalendarClient | None = None

    def __post_init__(self) -> None:
        if self.actions is None:
            self.actions = InMemoryPendingActionStore(self.settings.pending_action_ttl_seconds)
        if self.research is None:
            self.research = NullResearchClient()
        if self.tenant is None:
            self.tenant = NullTenantDraftClient()
        if self.memory is None:
            self.memory = NullMemoryStore()
        if self.voice is None:
            self.voice = NullVoiceStore()
        if self.users is None:
            self.users = NullUserStore()
        if self.triage_sessions is None:
            self.triage_sessions = InMemoryTriageStore(self.settings.state_ttl_seconds)
        if self.interpreter is None:
            self.interpreter = NullInterpreter()
        if self.file_source is None:
            self.file_source = NullFileSource()
        if self.local_file_source is None:
            self.local_file_source = NullFileSource()
        if self.calendar is None:
            self.calendar = NullCalendarClient()

    def handle(
        self, email: InboundEmail
    ) -> tuple[ComposedReply | None, str | None, ActionDescriptor | None]:
        self.store.purge()  # opportunistic retention sweep (KTD5); cheap for in-memory
        self.actions.purge()
        self.triage_sessions.purge()  # TR v2 sessions are content — sweep them on the same cadence
        # Three-way dispatch: a correlated reply is a confirm if its token is a pending
        # action (checked FIRST), else a revise; otherwise it's a fresh request. The two
        # token namespaces are disjoint by construction (separate stores, KTD-B).
        if self.settings.signup_local and email.secret_address_local == self.settings.signup_local:
            outcome = self._handle_signup(email)
        elif email.correlation_token and self.actions.get(email.correlation_token) is not None:
            outcome = self._handle_confirm(email)
        elif email.correlation_token and self.triage_sessions.get(email.correlation_token) is not None:
            outcome = self._handle_triage_followup(email)
        elif email.correlation_token:
            outcome = self._handle_revise(email)
        else:
            outcome = self._handle_new(email)
        # Action outcomes get their own metrics in U7; the draft event is for drafting.
        if outcome.result_kind not in (ResultKind.ACTION_SENT, ResultKind.ACTION_DECLINED):
            emit(
                "draft",
                ceo_id=email.secret_address_local,
                salt=self.settings.metrics_salt,
                revise=bool(email.correlation_token),
                result_kind=outcome.result_kind.name,
                action_code=outcome.action_code,
                deferred_code=outcome.deferred_code,
                nl_fallback=outcome.nl_fallback,
                revision_count=outcome.revision_count,
                recipient_uncertain=outcome.recipient_uncertain,
                clarifying=outcome.result_kind is ResultKind.CLARIFY,
                rung=outcome.rung,
            )
        reply = outcome.reply
        if reply is not None:
            reply = self._with_first_contact_notice(
                email, reply, outcome.result_kind, outcome.rung
            )
        return reply, outcome.reply_to, outcome.action

    def _with_first_contact_notice(
        self,
        email: InboundEmail,
        reply: ComposedReply,
        result_kind: ResultKind,
        rung: str | None = None,
    ) -> ComposedReply:
        # Only attach (and burn) the one-time disclosure on a real deliverable —
        # a first CLARIFY/LOST reply must not suppress it for the actual draft.
        if result_kind not in (ResultKind.DRAFT, ResultKind.SUMMARY):
            return reply
        sender = email.from_address.strip().lower()
        # Disclose ACCURATELY per processing location: an R3 draft is produced by a local
        # in-boundary model (never the external LLM), so it must not claim Claude. Dedup
        # per (sender, processing-kind) so each distinct disclosure is shown exactly once —
        # a CEO whose first draft is R3 still gets the external-LLM notice on their first R1 draft.
        local = rung == Rung.R3.value
        key = f"{sender}|{'local' if local else 'external'}"
        if key in self.notified:
            return reply
        self.notified.add(key)
        notice = _FIRST_CONTACT_NOTICE_LOCAL if local else _FIRST_CONTACT_NOTICE
        return ComposedReply(subject=reply.subject, body=reply.body + notice)

    # --- voice profile (VP) -------------------------------------------------

    def _handle_voice(self, email: InboundEmail) -> _HandlerResult:
        """VP: view the CEO's saved voice profile; 'VP FORGET' wipes it. Inspect/clear control
        for Layer B voice memory (R1; per-CEO)."""
        if "FORGET" in (email.body or "").upper():
            self.voice.forget(email.secret_address_local)
            notice = NoraResult(
                kind=ResultKind.CLARIFY, text="Forgotten — I've cleared your saved voice profile."
            )
            return _HandlerResult(compose_reply(notice), None, ResultKind.CLARIFY)
        profile = self.voice.profile(email.secret_address_local)
        text = profile or "I haven't learned a voice profile for you yet."
        notice = NoraResult(kind=ResultKind.CLARIFY, text=text)
        return _HandlerResult(compose_reply(notice), None, ResultKind.CLARIFY)

    # --- relationship memory (RM) -------------------------------------------

    def _handle_relationship_memory(self, email: InboundEmail, req: ParsedRequest) -> _HandlerResult:
        """RM: read back the saved facts about this thread's counterparty (curated vs auto-
        derived); 'RM FORGET' wipes them. Audit/clear control for Layer C (R1; owner-scoped).
        Read-back to the CEO only — never a draft, never sent to anyone."""
        counterparty = (req.thread.original_sender or "").strip()
        if not counterparty:
            notice = NoraResult(
                kind=ResultKind.CLARIFY,
                text="Tell me whose record you want — forward a thread from them, then send RM.",
            )
            return _HandlerResult(compose_reply(notice, req.thread.subject), None, ResultKind.CLARIFY)
        if "FORGET" in (email.body or "").upper():
            self.memory.forget_notes(email.secret_address_local, counterparty)
            notice = NoraResult(
                kind=ResultKind.CLARIFY,
                text=f"Forgotten — I've cleared everything I'd saved about {counterparty}.",
            )
            return _HandlerResult(compose_reply(notice, req.thread.subject), None, ResultKind.CLARIFY)
        mem = self.memory.recall(email.secret_address_local, counterparty)
        if not mem or (not mem.notes and not mem.derived):
            notice = NoraResult(
                kind=ResultKind.CLARIFY,
                text=f"I don't have any saved facts about {counterparty} yet.",
            )
            return _HandlerResult(compose_reply(notice, req.thread.subject), None, ResultKind.CLARIFY)
        lines = [f"What I know about {counterparty}:"]
        if mem.notes:
            lines.append("Confirmed (you told me):")
            lines += [f"  • {n}" for n in mem.notes]
        if mem.derived:
            lines.append("Observed (I inferred — unconfirmed):")
            lines += [f"  • {d}" for d in mem.derived]
        lines.append("Reply RM FORGET to clear all of this.")
        notice = NoraResult(kind=ResultKind.CLARIFY, text="\n".join(lines))
        return _HandlerResult(compose_reply(notice, req.thread.subject), None, ResultKind.CLARIFY)

    def _handle_revise(
        self, email: InboundEmail
    ) -> _HandlerResult:
        token = email.correlation_token or ""
        record = self.store.get(token)
        # Owner binding: the record is only revisable by the same secret address
        # that created it. A mismatch (or unknown token) is treated identically —
        # never reveal that a token exists for a different CEO (P0).
        if record is None or record.owner_secret_local != email.secret_address_local:
            notice = NoraResult(
                kind=ResultKind.LOST_THREAD,
                text="I couldn't match this to an earlier draft.",
            )
            return _HandlerResult(compose_reply(notice), None, ResultKind.LOST_THREAD)
        if record.origin_kind is not ResultKind.DRAFT:
            notice = NoraResult(
                kind=ResultKind.CLARIFY,
                text="That was a summary — nothing to revise. Forward a thread with "
                "WIYOW if you'd like a draft.",
            )
            return _HandlerResult(
                compose_reply(notice, record.thread.subject), None, ResultKind.CLARIFY
            )
        if record.revision_count >= self.settings.max_revise_rounds:
            self.store.delete(token)
            notice = NoraResult(
                kind=ResultKind.CLARIFY,
                text="We've revised this several times — take it from the latest draft, "
                "or forward the thread fresh to start over.",
            )
            return _HandlerResult(
                compose_reply(notice, record.thread.subject), None, ResultKind.CLARIFY
            )

        change_text, _ = split_authored_region(email.body)
        req = ParsedRequest(
            command=Command.WIYOW,
            instruction_text=change_text.strip() or None,
            thread=record.thread,
            recipient_uncertain=record.recipient_uncertain,
            correlation_token=token,
        )
        # KL stickiness: a revise on a keep-local draft stays on the on-device model.
        # Fail-closed — if no local backend is wired, decline; NEVER fall back to the Cloud.
        if record.keep_local:
            if self.local_drafting is None:
                notice = NoraResult(kind=ResultKind.CLARIFY, text=_HIGHER_ASSURANCE_TEXT)
                return _HandlerResult(
                    compose_reply(notice, record.thread.subject), None,
                    ResultKind.CLARIFY, rung="R3",
                )
            result = self._draft_or_fail(
                req, Intent.DRAFT, record.thread.subject, drafting=self.local_drafting
            )
        else:
            result = self._draft_or_fail(req, Intent.DRAFT, record.thread.subject)
        reply_to: str | None = None
        if result.kind is ResultKind.DRAFT:
            record.revision_count += 1
            record.created_at = time.time()  # slide the TTL window on activity
            self.store.put(record)
            reply_to = self._reply_to(email, token)
        reply = compose_reply(result, record.thread.subject)
        if record.keep_local and result.kind is ResultKind.DRAFT:
            reply = ComposedReply(subject=reply.subject, body=reply.body + _KEPT_LOCAL_RECEIPT)
        return _HandlerResult(
            reply,
            reply_to,
            result.kind,
            revision_count=record.revision_count,
            recipient_uncertain=record.recipient_uncertain,
            rung="R3" if record.keep_local else None,
        )

    # --- new request path ---------------------------------------------------

    def _handle_new(self, email: InboundEmail) -> _HandlerResult:
        req = parse_request(email)
        # Posture gate (must run BEFORE route/drafting so content never reaches the external
        # LLM). Keyed on original_sender, which is body-derived (v1.0 caveat, same as KTD9) —
        # so the decision is best-effort. The failure mode is safe: a spoofed/wrong counterparty
        # can cause a false-positive *decline* (over-cautious), never a bypass that leaks a
        # higher-assurance thread to the external path.
        # The KL ("keep local") code is an EXPLICIT per-email sensitivity signal from the
        # CEO: process entirely on the on-device model, never send to the Cloud API. It
        # forces local handling regardless of the counterparty's posture (and takes
        # precedence over it) — sensitivity rides the message, not the relationship.
        if "KL" in (req.modifier_codes or []):
            if req.action_code == "TR":
                # Triage is a Cloud call; never combine with keep-local — decline (fail-closed).
                notice = NoraResult(kind=ResultKind.CLARIFY, text=_TRIAGE_KL_DECLINE)
                return _HandlerResult(
                    compose_reply(notice, req.thread.subject), None, ResultKind.CLARIFY, rung="R3"
                )
            return self._handle_r3_local(email, req, keep_local=True)
        posture = resolve_posture(req.thread.original_sender, self.settings)
        if posture.rung is Rung.R2:
            return self._handle_r2_tenant(email, req)
        if posture.rung is Rung.R3:
            return self._handle_r3_local(email, req)
        if posture.rung is not Rung.R1:
            # Any future rung — decline (safe default). Content never reaches any LLM.
            notice = NoraResult(kind=ResultKind.CLARIFY, text=_HIGHER_ASSURANCE_TEXT)
            return _HandlerResult(
                compose_reply(notice, req.thread.subject), None, ResultKind.CLARIFY,
                rung=posture.rung.value,
            )
        # Plain language (no explicit code) → interpret it into the same fields a code sets, then
        # route normally. KL/posture already returned above, so the cloud interpreter is only ever
        # reached on the R1 path. An explicit code skips this entirely (deterministic fast path).
        # The interpreter rewrites instruction_text into its paraphrase (ir.notes), which can
        # drop a file reference ("the file of X" → "retrieve the X file" or terser). Keep the
        # raw instruction so the file-ref extraction below can fall back to it.
        pre_interp_instruction = req.instruction_text
        if req.action_code is None and req.deferred_code is None:
            ir = self.interpreter.interpret(req.instruction_text or req.thread.body or "")
            if ir.intent == "unclear" and ir.clarify:
                notice = NoraResult(kind=ResultKind.CLARIFY, text=ir.clarify)
                return _HandlerResult(compose_reply(notice, req.thread.subject), None, ResultKind.CLARIFY)
            req = apply_interpretation(req, ir)
        intent, question = route(req)
        if intent is Intent.CLARIFY:
            result = NoraResult(kind=ResultKind.CLARIFY, text=question or "")
            return _HandlerResult(
                compose_reply(result, req.thread.subject), None, ResultKind.CLARIFY
            )
        if intent is Intent.DEFERRED:
            result = self._deferred_result(req)
            return _HandlerResult(
                compose_reply(result, req.thread.subject), None, ResultKind.DEFERRED
            )
        if intent is Intent.VOICE_PROFILE:
            return self._handle_voice(email)
        if intent is Intent.RELATIONSHIP_MEMORY:
            return self._handle_relationship_memory(email, req)
        if intent is Intent.HELP:
            notice = NoraResult(kind=ResultKind.CLARIFY, text=HELP_TEXT)
            return _HandlerResult(compose_reply(notice, req.thread.subject), None, ResultKind.CLARIFY)
        if intent is Intent.TRIAGE:
            return self._handle_triage(email, req)
        if intent is Intent.PROPOSE_MEETING:
            return self._handle_propose(email, req)
        if intent is Intent.PROPOSE_CALENDAR:
            return self._handle_propose_calendar(email, req)
        if intent is Intent.RESEARCH:
            return self._handle_research(email, req)
        if intent is Intent.PROPOSE_SHARE:
            return self._handle_share(email, req)

        # Relationship-memory recall (R1, Layer A): a metadata-only breadcrumb fed to the
        # draft as fenced DATA. Reached only on the R1 path — KL/R3/R2 return earlier, so
        # nothing keep-local/in-tenant is ever recorded to or recalled from this store.
        counterparty = (req.thread.original_sender or "").strip()
        # Layer C: a CEO-curated relationship directive ([remember: …] / [forget]). Parsed out so
        # the rest of the instruction still drives the draft; applied (stored/cleared) after.
        rel_action, rel_fact, rel_remaining = parse_relationship_directive(req.instruction_text)
        if rel_action is not None:
            req.instruction_text = rel_remaining or None
        if counterparty:
            ctx = _relationship_context(self.memory.recall(email.secret_address_local, counterparty))
            if ctx:
                req.relationship_context = ctx
        # Layer C v2 (auto-learn): invite derivation on this R1 draft only. KL/R3/R2 already
        # returned above, so this flag never rides off the R1 path (fail-closed). Off by default.
        req.autolearn = self.settings.relationship_autolearn
        # Voice memory (R1, Layer B): the CEO's consolidated style profile, from coded signals
        # only. Same R1-only structural guarantee as Layer A. v2: prefer the per-recipient
        # profile (formal to the board, casual to a teammate), falling back to the global one.
        voice_profile = (
            self.voice.profile(email.secret_address_local, counterparty=counterparty)
            or self.voice.profile(email.secret_address_local)
        )
        if voice_profile:
            req.voice_profile = voice_profile
        # Files-domain access (Layer A): a referenced company file ('file: NAME') is read
        # from the connected store into the draft context. Read-only, no consent gate (the
        # principal's own file → their own draft); default-off via NullFileSource.
        ref = (
            _file_ref(req.instruction_text) or _nl_file_descriptor(req.instruction_text)
            or _file_ref(pre_interp_instruction) or _nl_file_descriptor(pre_interp_instruction)
        )
        if ref:
            candidates, file_ctx = _resolve_referenced_file(self.file_source, ref)
            if len(candidates) > 1:
                # Don't guess among matches — list them and ask which (shot-list "no
                # wrong file"). Deterministic clarify; never reads/summarizes one.
                listed = "\n".join(f"  • {c}" for c in candidates)
                notice = NoraResult(
                    kind=ResultKind.CLARIFY,
                    interpretation_readback=(
                        f'Several files match "{ref}" — I need you to pick which one.'
                    ),
                    text=(f'Several files match "{ref}" — I won\'t guess which:\n{listed}'
                          "\n\nReply with the exact name and I'll read that one."),
                )
                return _HandlerResult(
                    compose_reply(notice, req.thread.subject), None, ResultKind.CLARIFY,
                )
            req.file_context = file_ctx
        result = self._draft_or_fail(req, intent, req.thread.subject)
        # Layer C (R1-only): apply the CEO-curated relationship directive. Owner-scoped; facts
        # ride only into this CEO's own future drafts (never emitted to a third party).
        if counterparty and rel_action == "remember" and rel_fact:
            self.memory.remember(email.secret_address_local, counterparty, rel_fact)
        elif counterparty and rel_action == "forget":
            self.memory.forget_notes(email.secret_address_local, counterparty)
        # Layer C v2 (auto-learn): fold newly-derived facts into memory (R1-only). Skip when the
        # CEO just issued [forget] this turn. result.derived_facts is rewritten to ONLY the
        # newly-learned facts so the receipt surfaces exactly what changed (Task 5).
        newly_learned: list[str] = []
        if (
            self.settings.relationship_autolearn
            and counterparty
            and rel_action != "forget"
            and result.derived_facts
        ):
            mem_now = self.memory.recall(email.secret_address_local, counterparty)
            known = (
                {f.lower() for f in (mem_now.notes + mem_now.derived)} if mem_now else set()
            )
            newly_learned = [f for f in result.derived_facts if f.lower() not in known]
            if newly_learned:
                self.memory.learn(email.secret_address_local, counterparty, newly_learned)
        result.derived_facts = newly_learned
        reply_to: str | None = None
        if result.kind is ResultKind.DRAFT:
            token = new_correlation_token()
            self.store.put(
                ReviseRecord(
                    token=token,
                    owner_secret_local=email.secret_address_local,
                    thread=req.thread,
                    origin_kind=ResultKind.DRAFT,
                    recipient_uncertain=req.recipient_uncertain,
                )
            )
            reply_to = self._reply_to(email, token)
            if counterparty:
                self.memory.record(
                    email.secret_address_local, counterparty, datetime.now(timezone.utc).date().isoformat()
                )
            # Voice memory (Layer B): record the coded refinement signals used (R1 only).
            # Record globally (keeps the fallback profile fresh) and, when the recipient is
            # known, under that counterparty too (Layer B v2 per-recipient style).
            now_ts = datetime.now(timezone.utc).timestamp()
            self.voice.record(email.secret_address_local, req.modifier_codes or [], now_ts)
            if counterparty:
                self.voice.record(
                    email.secret_address_local, req.modifier_codes or [], now_ts,
                    counterparty=counterparty,
                )
        reply = compose_reply(result, req.thread.subject)
        _attach_report_file(reply, result, intent, req.modifier_codes)
        # PR.SV: also file the generated report into Drive (her own store, no third party)
        # and append the link. Best-effort — a failure leaves the inline/attached report intact.
        if intent is Intent.REPORT and "SV" in (req.modifier_codes or []):
            fmt = next((m for m in req.modifier_codes if m in SUPPORTED_REPORT_FORMATS), "W")
            try:
                art = render_report(result.text, fmt)
                link = self.file_source.create(art.filename, art.content, art.content_type)
                if link:
                    reply = ComposedReply(
                        subject=reply.subject, body=f"{reply.body}\n\nSaved to Drive: {link}"
                    )
            except Exception:  # noqa: BLE001 — saving is best-effort
                logger.exception("save-to-drive failed")
        return _HandlerResult(
            reply,
            reply_to,
            result.kind,
            action_code=req.action_code,
            deferred_code=req.deferred_code,
            nl_fallback=req.action_code is None and req.deferred_code is None,
            recipient_uncertain=req.recipient_uncertain,
        )

    # --- R2 in-tenant draft path --------------------------------------------

    def _handle_r2_tenant(self, email: InboundEmail, req: ParsedRequest) -> _HandlerResult:
        """R2: keep mail in the client's tenant. Draft, then save to their Drafts folder.
        CRITICAL: if no real tenant backend is configured, decline WITHOUT drafting —
        R2 content must not hit the (non-DPA) LLM on R1 infra (default-off guarantee)."""
        if isinstance(self.tenant, NullTenantDraftClient):
            notice = NoraResult(kind=ResultKind.CLARIFY, text=_HIGHER_ASSURANCE_TEXT)
            return _HandlerResult(
                compose_reply(notice, req.thread.subject), None, ResultKind.CLARIFY, rung="R2"
            )
        result = self._draft_or_fail(req, Intent.DRAFT, req.thread.subject)
        if result.kind is not ResultKind.DRAFT:
            return _HandlerResult(
                compose_reply(result, req.thread.subject), None, result.kind, rung="R2"
            )
        try:
            self.tenant.save_draft(
                to=req.thread.original_sender or "",
                subject=req.thread.subject or "Re:",
                body=result.text,
            )
        except TenantUnavailable:
            notice = NoraResult(kind=ResultKind.CLARIFY, text=_HIGHER_ASSURANCE_TEXT)
            return _HandlerResult(
                compose_reply(notice, req.thread.subject), None, ResultKind.CLARIFY, rung="R2"
            )
        reply = ComposedReply(
            subject=f"Nora · drafted in your tenant — {req.thread.subject or 'your thread'}",
            body=(
                "I drafted this into your tenant's Drafts folder — it stayed in your tenant; "
                "review and send it from there.\n\n— Nora —"
            ),
        )
        return _HandlerResult(reply, None, ResultKind.DRAFT, rung="R2")

    # --- R3 local (in-boundary) draft path ----------------------------------

    def _handle_r3_local(
        self, email: InboundEmail, req: ParsedRequest, keep_local: bool = False
    ) -> _HandlerResult:
        """Highest-assurance / local-only path. Reached either by an R3 counterparty posture
        or by the explicit KL ("keep local") code (``keep_local=True``). If a local inference
        backend is configured, draft the PRODUCING intents on local hardware (never the
        external Cloud API); else decline. Third-party-egress intents (propose-meeting /
        calendar / research) still decline here in this version — egress is the deferred
        role-gated boundary. CRITICAL: with no local backend we decline BEFORE route()/
        produce(), so sensitive content never touches any external LLM."""
        if self.local_drafting is None:
            notice = NoraResult(kind=ResultKind.CLARIFY, text=_HIGHER_ASSURANCE_TEXT)
            return _HandlerResult(
                compose_reply(notice, req.thread.subject), None, ResultKind.CLARIFY, rung="R3"
            )
        intent, _question = route(req)
        if intent not in (Intent.DRAFT, Intent.SUMMARIZE, Intent.ANALYZE, Intent.REPORT):
            notice = NoraResult(kind=ResultKind.CLARIFY, text=_HIGHER_ASSURANCE_TEXT)
            return _HandlerResult(
                compose_reply(notice, req.thread.subject), None, ResultKind.CLARIFY, rung="R3"
            )
        # R3 file access (in-boundary only): a referenced file is read from the local
        # FileVault — never the Cloud Drive/OneDrive source — so content stays on-device.
        ref = _file_ref(req.instruction_text) or _nl_file_descriptor(req.instruction_text)
        if ref:
            req.file_context = _read_referenced_file(self.local_file_source, ref)
        result = self._draft_or_fail(
            req, intent, req.thread.subject, drafting=self.local_drafting
        )
        reply = compose_reply(result, req.thread.subject)
        reply_to: str | None = None
        if keep_local and result.kind is ResultKind.DRAFT:
            # KL stickiness: store a keep_local revise record + correlation token so a reply
            # to this draft round-trips back through the on-device model (never the Cloud).
            token = new_correlation_token()
            self.store.put(
                ReviseRecord(
                    token=token,
                    owner_secret_local=email.secret_address_local,
                    thread=req.thread,
                    origin_kind=ResultKind.DRAFT,
                    recipient_uncertain=req.recipient_uncertain,
                    keep_local=True,
                )
            )
            reply_to = self._reply_to(email, token)
        # Per-email receipt when the CEO explicitly marked the message KL: confirm, every
        # time, that this specific content stayed on-device (the first-contact notice fires
        # only once; sensitive handling warrants a standing confirmation).
        if keep_local and result.kind in (ResultKind.DRAFT, ResultKind.SUMMARY):
            reply = ComposedReply(subject=reply.subject, body=reply.body + _KEPT_LOCAL_RECEIPT)
        return _HandlerResult(reply, reply_to, result.kind, rung="R3")

    # --- confirm path (consent-gated action, 0.2.0) -------------------------

    def _handle_confirm(self, email: InboundEmail) -> _HandlerResult:
        """Handle a CEO reply correlated to a pending action. Uses ONLY the stored
        record (zero re-parse of the confirm body/quoted text — KTD-F); reads the
        AG/ANG reaction from the authored region; consumes BEFORE returning a send
        descriptor (KTD-D). Never sends here."""
        token = email.correlation_token or ""
        record = self.actions.get(token)
        # Owner binding + unknown → lost-thread; never reveal another CEO's token (P0).
        if record is None or record.owner_secret_local != email.secret_address_local:
            notice = NoraResult(
                kind=ResultKind.LOST_THREAD, text="I couldn't match this to a pending proposal."
            )
            return _HandlerResult(compose_reply(notice), None, ResultKind.LOST_THREAD)
        # Replay: already consumed → no-op, never a second send.
        if record.status != "pending":
            done = "scheduled" if record.action_kind == "calendar_hold" else "sent"
            notice = NoraResult(
                kind=ResultKind.ACTION_SENT,
                text=f"Already handled — that proposal to {record.recipient} was {done}.",
                recipient=record.recipient,
            )
            return _HandlerResult(compose_reply(notice, record.subject), None, ResultKind.ACTION_SENT)

        # Read the reaction from the AUTHORED region only (never quoted text).
        authored, _ = split_authored_region(email.body)
        cp, instruction = parse_command_line(authored)
        has_change = bool(instruction and instruction.strip())

        if cp.approval == "decline":
            self.actions.delete(token)
            self._emit_action(email, "declined")
            notice = NoraResult(
                kind=ResultKind.ACTION_DECLINED,
                text=f"Cancelled — I won't send anything to {record.recipient}.",
                recipient=record.recipient,
            )
            return _HandlerResult(
                compose_reply(notice, record.subject), None, ResultKind.ACTION_DECLINED
            )
        if cp.approval == "confirm" and not has_change:
            # Single-use consume. Calendar holds (no durable outbox) consume HERE, before the
            # create. Email/share sends consume in main AFTER the outbox.record
            # (write-before-consume), so a crash in that window leaves the record pending and the
            # approved send is recovered by the outbox replay / a re-AG — never silently lost. A
            # double-send is still prevented by the idempotency-keyed send (KTD-D).
            if record.action_kind == "calendar_hold":
                record.status = "consumed"
                self.actions.put(record)
            self._emit_action(email, "confirmed")
            descriptor = ActionDescriptor(
                recipient=record.recipient, subject=record.subject, body=record.body, token=token,
                action_kind=record.action_kind, payload=record.payload,
            )
            # No CEO reply here — main composes the outcome receipt AFTER the send (U6).
            return _HandlerResult(None, None, ResultKind.ACTION_SENT, action=descriptor)
        # Confirm-with-modification, or no clear reaction → ask; never send the stale
        # payload (KTD-F). The proposal stays pending for a clean AG/ANG.
        text = (
            "To send that proposal as I showed it, reply just AG. To cancel, reply ANG. "
            "If you want changes, forward the thread again with your edits and I'll re-propose."
            if has_change
            else "Reply AG to send that proposal as shown, or ANG to cancel."
        )
        notice = NoraResult(kind=ResultKind.CLARIFY, text=text)
        return _HandlerResult(compose_reply(notice, record.subject), None, ResultKind.CLARIFY)

    # --- propose path (consent-gated action, 0.2.0) -------------------------

    def _handle_propose(self, email: InboundEmail, req: ParsedRequest) -> _HandlerResult:
        """Draft a meeting-proposal email and store it as a pending action awaiting
        the CEO's AG confirmation. NEVER sends here (KTD-D: send is U6, post-confirm)."""
        target = (req.thread.original_sender or "").strip()
        # Recipient safety (KTD-G): never guess. Ask the CEO if the target is uncertain
        # or the thread has other participants beyond the sender (ambiguous target).
        if req.recipient_uncertain or not target or self._has_other_participants(email, req):
            result = NoraResult(
                kind=ResultKind.CLARIFY,
                text="Who should I propose the meeting to? Reply with the exact email "
                "address and I'll prepare a proposal for you to approve.",
            )
            return _HandlerResult(
                compose_reply(result, req.thread.subject), None, ResultKind.CLARIFY,
                action_code=req.action_code,
            )

        result = self._draft_or_fail(req, Intent.PROPOSE_MEETING, req.thread.subject)
        if result.kind is not ResultKind.PROPOSAL:  # drafting failed → surface, no record
            return _HandlerResult(
                compose_reply(result, req.thread.subject), None, result.kind,
                action_code=req.action_code,
            )
        recipient = result.recipient or target
        proposal_subject = (
            f"Meeting request: {req.thread.subject}" if req.thread.subject else "Meeting request"
        )
        token = new_correlation_token()
        self.actions.put(
            PendingActionRecord(
                token=token,
                owner_secret_local=email.secret_address_local,
                recipient=recipient,
                subject=proposal_subject,
                body=result.text,
            )
        )
        self._emit_action(email, "proposed")
        return _HandlerResult(
            compose_reply(result, req.thread.subject),
            self._reply_to(email, token),
            ResultKind.PROPOSAL,
            action_code=req.action_code,
        )

    # --- propose-share path (consent-gated, SH) ----------------------------

    def _handle_share(self, email: InboundEmail, req: ParsedRequest) -> _HandlerResult:
        """Propose sharing a referenced company file with the thread's counterparty, as a
        consent-gated email attachment from Nora's OWN labeled address (sent only after AG).
        The file is re-fetched at send time (KTD-D) — never persisted in the pending record."""
        def _clarify(text: str) -> _HandlerResult:
            return _HandlerResult(
                compose_reply(NoraResult(kind=ResultKind.CLARIFY, text=text), req.thread.subject),
                None, ResultKind.CLARIFY, action_code=req.action_code,
            )

        ref = _file_ref(req.instruction_text)
        if not ref:
            return _clarify("Which file should I share? Reference it like [file: NAME].")
        target = (req.thread.original_sender or "").strip()
        # Recipient safety (KTD-G): never guess who to send a file to.
        if req.recipient_uncertain or not target or self._has_other_participants(email, req):
            return _clarify(
                "Who should I share this file with? Reply with the exact email address "
                "and I'll prepare it for your approval."
            )
        # Disambiguate by NAME only (metadata, no download, no grant-before-AG): if the
        # reference matches several files, never guess which to share — list them and ask.
        candidates = self.file_source.search(ref)
        if not candidates:
            return _clarify(f"I couldn't find a file named '{ref}' to share — check the name.")
        if len(candidates) > 1:
            listed = ", ".join(candidates)
            return _clarify(
                f"I found several files matching '{ref}': {listed}. "
                "Reply with the exact name and I'll prepare the share."
            )
        # Exactly one match — share that exact file (stored as the resolved name, so the
        # send-time fetch/grant is unambiguous). SH.G grants store access + a link;
        # plain SH attaches a copy.
        grant = "G" in (req.modifier_codes or [])
        filename = candidates[0]
        ref = filename

        share_subject = req.thread.subject or f"Sharing {filename}"
        share_body = (
            f"Hi,\n\nSharing {filename} with you, as requested"
            + (" — access link below." if grant else ".")
            + "\n\nBest,\nNora Bennett\nExecutive Assistant"
        )
        token = new_correlation_token()
        self.actions.put(
            PendingActionRecord(
                token=token,
                owner_secret_local=email.secret_address_local,
                recipient=target,
                subject=share_subject,
                body=share_body,
                action_kind="share_file",
                payload={"file_ref": ref, "share_mode": "grant" if grant else "attach"},
            )
        )
        self._emit_action(email, "proposed")
        how = (
            f"grant {target} access to {filename} in your store and email them the link"
            if grant else f"send {filename} to {target} as an attachment"
        )
        proposal = ComposedReply(
            subject=f"Nora · proposed file share — {filename} to {target}",
            body=(
                f"Proposed: {how}, from my own assistant address, with this note:\n\n"
                f"{share_body}\n\n"
                "Reply AG to share it, or ANG to cancel. Nothing is sent until you reply AG."
            ),
        )
        return _HandlerResult(
            proposal, self._reply_to(email, token), ResultKind.PROPOSAL, action_code=req.action_code
        )

    # --- propose-calendar path (consent-gated, SM.A / BM.A) ----------------

    def _handle_propose_calendar(self, email: InboundEmail, req: ParsedRequest) -> _HandlerResult:
        """Propose a calendar hold deterministically (no LLM). Stores a pending
        ``calendar_hold`` action awaiting AG. Never creates an event here (KTD-D)."""
        target = (req.thread.original_sender or "").strip()
        # Recipient safety (KTD-G): ask if target is uncertain or ambiguous.
        if req.recipient_uncertain or not target or self._has_other_participants(email, req):
            result = NoraResult(
                kind=ResultKind.CLARIFY,
                text="Who should I schedule a hold with? Reply with the exact email "
                "address and I'll prepare a calendar hold for you to approve.",
            )
            return _HandlerResult(
                compose_reply(result, req.thread.subject), None, ResultKind.CLARIFY,
                action_code=req.action_code,
            )

        summary = (
            f"Meeting: {req.thread.subject}" if req.thread.subject else "Meeting"
        )
        attendee = target
        # Resolve a concrete time from the CEO's instruction when given; otherwise keep
        # the placeholder (the hold then declines until a time is provided).
        placeholder = "(time to be confirmed)"
        slot = parse_slot(
            req.instruction_text or "", now=datetime.now(timezone.utc), tz=self.settings.timezone_name
        )
        start, end = slot if slot else (placeholder, placeholder)
        when = "the time you gave" if slot else "(time to be confirmed)"
        # Conflict-aware scheduling: read-only heads-up if the proposed window overlaps an
        # existing event. Only for a concrete time; fail-safe (any read error ⇒ no heads-up,
        # the proposal still goes out). Null calendar ⇒ []. Booking still waits for AG.
        conflict_line = ""
        if slot:
            try:
                conflicts = self.calendar.busy(start, end)
            except Exception:  # noqa: BLE001 — never let a conflict check break the propose path
                conflicts = []
            if conflicts:
                names = "; ".join(conflicts[:3])
                conflict_line = (
                    f"\n\n⚠ Heads up: your calendar already shows {names} at that time — "
                    "reply AG to hold anyway, or send a different time."
                )
        # IV ("invite") makes this a consent-gated invitation to the participant; without
        # it the default is a PRIVATE hold (nobody notified). The invite, if confirmed, is
        # organized from Nora's own labeled calendar identity — never as the principal.
        invite = "IV" in (req.modifier_codes or [])
        action_line = (
            f"invite {attendee} and place this hold" if invite else "place this private hold"
        )
        hold_description = (
            f"Proposed calendar hold — {'invite + hold for' if invite else 'private hold, no invite —'} {attendee}\n"
            f"Summary: {summary}\n"
            f"Start: {start}\n"
            f"End: {end}{conflict_line}\n\n"
            f"Note: {when}. Reply AG to {action_line}, or ANG to cancel."
        )
        token = new_correlation_token()
        self.actions.put(
            PendingActionRecord(
                token=token,
                owner_secret_local=email.secret_address_local,
                recipient=attendee,
                subject=summary,
                body=hold_description,
                action_kind="calendar_hold",
                payload={
                    "summary": summary, "start": start, "end": end,
                    "attendees": [attendee], "invite": invite,
                },
            )
        )
        self._emit_action(email, "proposed")
        # Calendar-specific receipt — NOT the email-proposal copy (no "send"/"assistant
        # address"/copy-markers; a hold is created on the calendar, not emailed).
        subj = req.thread.subject or "your thread"
        reply = ComposedReply(
            subject=f"Nora · proposed calendar hold with {attendee} — {subj}",
            body=f"{hold_description}\n\n— Nora —",
        )
        return _HandlerResult(
            reply,
            self._reply_to(email, token),
            ResultKind.PROPOSAL,
            action_code=req.action_code,
        )

    # --- ambient auto-draft (connected mailbox) --------------------------------

    def autodraft_reply(
        self, owner_local: str, sender: str, subject: str, body: str
    ) -> NoraResult | None:
        """Produce a reply DRAFT to a direct inbound from ``sender`` for the mailbox owner
        ``owner_local`` — voice + relationship aware, R1-only, **never sends**. The caller saves
        the result to the owner's Drafts. Returns None when the sender isn't R1 (content must not
        auto-flow to the cloud LLM) or no draft was produced. No correlation token, no consent
        record, no receipt — this path only drafts."""
        sender = (sender or "").strip()
        if not sender or resolve_posture(sender, self.settings).rung is not Rung.R1:
            return None
        req = ParsedRequest(
            thread=ParsedThread(body=body or "", original_sender=sender, subject=subject or "")
        )
        ctx = _relationship_context(self.memory.recall(owner_local, sender))
        if ctx:
            req.relationship_context = ctx
        vp = self.voice.profile(owner_local, counterparty=sender) or self.voice.profile(owner_local)
        if vp:
            req.voice_profile = vp
        req.autolearn = self.settings.relationship_autolearn
        result = self._draft_or_fail(req, Intent.DRAFT, subject)
        if result.kind is not ResultKind.DRAFT:
            return None
        # Keep memory consistent with the forward path: fold newly-derived facts + breadcrumb.
        if self.settings.relationship_autolearn and result.derived_facts:
            mem_now = self.memory.recall(owner_local, sender)
            known = {f.lower() for f in (mem_now.notes + mem_now.derived)} if mem_now else set()
            new = [f for f in result.derived_facts if f.lower() not in known]
            if new:
                self.memory.learn(owner_local, sender, new)
            result.derived_facts = new
        self.memory.record(owner_local, sender, datetime.now(timezone.utc).date().isoformat())
        return result

    # --- research path (FI/I, pluggable backend, default-off) ------------------

    def _handle_signup(self, email: InboundEmail) -> _HandlerResult:
        """R1 self-serve onboarding. DKIM is already proven upstream; enforce the invite
        allowlist, dedup, provision a private address, and reply to the signer. No consent
        gate, no third-party send (the reply goes to the DKIM-verified signer)."""
        if not signup_eligible(email.from_address, self.settings.signup_allowlist):
            return _HandlerResult(access_request_reply(email.from_address), None, ResultKind.CLARIFY)
        secret_local, _is_new = provision_signup(self.users, email.from_address)
        domain = (self.settings.postmark_sender or "nora@nora-bennett.com").split("@", 1)[-1]
        return _HandlerResult(welcome_reply(secret_local, domain), None, ResultKind.CLARIFY)

    def _handle_triage(self, email: InboundEmail, req: ParsedRequest) -> _HandlerResult:
        """TR: split a bundled forward into per-thread items and return a priority-ranked,
        read-only digest. No revise token, no consent record (mirrors SUMMARIZE)."""
        req.triage_items = split_threads(req.thread.body or "")
        if not req.triage_items:
            notice = NoraResult(
                kind=ResultKind.CLARIFY,
                text="I didn't find any forwarded threads to triage — forward the threads with TR.",
            )
            return _HandlerResult(compose_reply(notice, req.thread.subject), None, ResultKind.CLARIFY)
        # Triage v2: annotate each thread with its sender's familiarity (R1, owner-scoped). Only
        # when memory is on (NullMemoryStore ⇒ skip, digest unchanged). Curated facts only.
        if not isinstance(self.memory, NullMemoryStore):
            hints: list[str] = []
            for item in req.triage_items:
                sender = (parse_forwarded_thread(item).original_sender or "").strip()
                if not sender:
                    hints.append("")
                    continue
                mem = self.memory.recall(email.secret_address_local, sender)
                hints.append(relationship_hint(
                    mem.thread_count if mem else 0,
                    mem.last_seen if mem else "",
                    mem.notes if mem else [],
                ))
            req.triage_relationship_hints = hints
        result = self._draft_or_fail(req, Intent.TRIAGE, req.thread.subject)
        # Persist the split items under a correlation token + set a plus-addressed reply-to, so a
        # reply like "2 RE" can act on item 2 without re-forwarding (TR v2). Read-only; the items
        # are the CEO's own forwarded content, transient (TTL) and owner-bound.
        reply_to: str | None = None
        if result.kind is not ResultKind.CLARIFY:
            token = new_correlation_token()
            self.triage_sessions.put(
                TriageSessionRecord(
                    token=token,
                    owner_secret_local=email.secret_address_local,
                    items=req.triage_items,
                    subject=req.thread.subject or "",
                )
            )
            reply_to = self._reply_to(email, token)
        return _HandlerResult(compose_reply(result, req.thread.subject), reply_to, result.kind)

    def _handle_triage_followup(self, email: InboundEmail) -> _HandlerResult:
        """TR v2: a reply to a triage digest. Parse '<n> <ACTION> [notes]' lines and produce each
        on the stored item — read-only, drafts back to the CEO only (no consent path, no
        third-party send). Out-of-range / unsupported references degrade to an inline note so we
        never act on the wrong thread. The session stays alive (reply-to re-set) for more items."""
        record = self.triage_sessions.get(email.correlation_token or "")
        if record is None or record.owner_secret_local != email.secret_address_local:
            notice = NoraResult(
                kind=ResultKind.CLARIFY,
                text="That triage digest has expired — forward the threads again with TR.",
            )
            return _HandlerResult(compose_reply(notice, email.subject), None, ResultKind.CLARIFY)
        # Parse the AUTHORED region only — never numbers/codes from the quoted digest below.
        authored, _ = split_authored_region(email.body)
        refs = parse_triage_followup(authored or "", len(record.items))
        reply_to = self._reply_to(email, record.token)
        if not refs:
            notice = NoraResult(
                kind=ResultKind.CLARIFY,
                text=(
                    "Tell me which items to act on, one per line — e.g. '2 RE' to draft a reply to "
                    "item 2, or '4 S' to summarize item 4. Codes: RE, S, AC, PR."
                ),
            )
            return _HandlerResult(compose_reply(notice, email.subject), reply_to, ResultKind.CLARIFY)
        sections: list[str] = []
        for ref in refs:
            if not ref.in_range:
                sections.append(f"--- item {ref.index}: no such item (the digest had {len(record.items)}) ---")
                continue
            if not ref.supported:
                sections.append(
                    f"--- item {ref.index} ({ref.action_code}): not available per-item — "
                    f"forward that thread on its own with {ref.action_code} to use it ---"
                )
                continue
            item_req = ParsedRequest(
                action_code=ref.action_code,
                instruction_text=ref.notes,
                raw_code_line=ref.raw_code or None,
                thread=ParsedThread(body=record.items[ref.index - 1], subject=record.subject),
            )
            intent, _ = route(item_req)
            result = self._draft_or_fail(item_req, intent, record.subject)
            sections.append(f"--- item {ref.index} ({ref.action_code}) ---\n{result.text}")
        combined = NoraResult(
            kind=ResultKind.SUMMARY,
            text="\n\n".join(sections),
            interpretation_readback="Acting on your chosen items from the triage digest.",
        )
        return _HandlerResult(compose_reply(combined, record.subject), reply_to, ResultKind.SUMMARY)

    def _handle_research(self, email: InboundEmail, req: ParsedRequest) -> _HandlerResult:
        """Route FI/I to the pluggable ResearchClient. Falls back to a graceful notice
        when no backend is configured (NullResearchClient — default-off). Research
        findings are returned to the CEO only; no third-party send (no consent gate)."""
        query = f"{req.thread.subject or ''}\n{req.thread.body[:1000]}".strip()
        try:
            findings = self.research.research(query)
        except ResearchUnavailable:
            notice = NoraResult(
                kind=ResultKind.CLARIFY,
                text=(
                    "I can't research on the web in this version — I work from what you "
                    "forward. Forward the thread with RE/S/AC/PR and I'll help with that."
                ),
            )
            return _HandlerResult(
                compose_reply(notice, req.thread.subject), None,
                ResultKind.CLARIFY, action_code=req.action_code,
            )
        result = NoraResult(
            kind=ResultKind.REPORT,
            text=findings,
            interpretation_readback="Research the people/company on the thread.",
        )
        return _HandlerResult(
            compose_reply(result, req.thread.subject), None,
            ResultKind.REPORT, action_code=req.action_code,
        )

    def _has_other_participants(self, email: InboundEmail, req: ParsedRequest) -> bool:
        """True when the thread has recipients beyond the sender and the CEO — the
        meeting target is ambiguous (KTD-G), so Nora must ask rather than guess."""
        ceo_addrs = self._ceo_addresses(email)
        sender = (req.thread.original_sender or "").lower()
        others = {
            r.lower()
            for r in req.thread.recipients
            if r.lower() and r.lower() not in ceo_addrs and r.lower() != sender
        }
        return bool(others)

    def _ceo_addresses(self, email: InboundEmail) -> set[str]:
        registered = self.settings.ceo_registry.get(email.secret_address_local)
        if registered is None:
            return set()
        if isinstance(registered, str):
            return {registered.lower()}
        return {a.lower() for a in registered}

    # --- helpers ------------------------------------------------------------

    def _deferred_result(self, req: ParsedRequest) -> NoraResult:
        """A recognized code Nora can't act on in this version — never act, explain."""
        label = DEFERRED_LABEL.get(req.deferred_code or "", "do that")
        text = (
            f"I understood you'd like me to {label}, but that isn't available in this "
            "version. I prepare text only — I can draft a reply (RE), summarize (SC/S), "
            "analyze (AC), or write a report (PR). I never send, book, or schedule on your "
            "behalf. Forward the thread with one of those codes and I'll help."
        )
        return NoraResult(
            kind=ResultKind.DEFERRED,
            text=text,
            interpretation_readback=f"You asked me to {label}.",
        )

    def _draft_or_fail(
        self,
        req: ParsedRequest,
        intent: Intent,
        subject: str | None,
        drafting: Drafting | None = None,
    ) -> NoraResult:
        # `drafting` lets a caller draft through an alternate backend (e.g. the local
        # in-boundary model for R3); defaults to the external Claude backend.
        backend = drafting or self.drafting
        try:
            return backend.produce(req, intent)
        except DraftingError:
            return NoraResult(
                kind=ResultKind.CLARIFY,
                text="I couldn't draft that just now — please try again.",
            )

    def _emit_action(self, email: InboundEmail, phase: str) -> None:
        """Emit a redacted consent-gated-action metric. Only the salted ceo_hash and a
        non-sensitive ``phase`` (proposed / confirmed / declined) — never the recipient,
        subject, or body (the ``sent`` / ``failed`` phases are emitted by main after I/O)."""
        emit(
            "action",
            ceo_id=email.secret_address_local,
            salt=self.settings.metrics_salt,
            phase=phase,
        )

    def _reply_to(self, email: InboundEmail, token: str) -> str:
        return build_secret_address(
            email.secret_address_local, token, self.settings.address_domain
        )
