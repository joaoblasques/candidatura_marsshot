"""Internal domain models shared across the request pipeline.

These are the normalized shapes the pipeline passes between units, kept
independent of any email-provider payload format so the provider stays
swappable (KTD2).
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class Command(str, Enum):
    """Recognized command codes (KTD8). ``NONE`` means a natural-language request."""

    WIYOW = "WIYOW"  # write in your own words — draft a reply
    S = "S"  # summarize the thread
    NONE = "NONE"  # no code present; interpret from plain language


class Intent(str, Enum):
    """Routing outcome produced by the interpreter (U4)."""

    DRAFT = "DRAFT"
    SUMMARIZE = "SUMMARIZE"
    ANALYZE = "ANALYZE"
    REPORT = "REPORT"
    PROPOSE_MEETING = "PROPOSE_MEETING"  # consent-gated meeting-proposal action (0.2.0)
    PROPOSE_CALENDAR = "PROPOSE_CALENDAR"  # consent-gated calendar-hold action (SM.A)
    RESEARCH = "RESEARCH"  # pluggable web-research backend (FI/I); default-off (NullResearchClient)
    PROPOSE_SHARE = "PROPOSE_SHARE"  # consent-gated file share (SH); from Nora's own address
    VOICE_PROFILE = "VOICE_PROFILE"  # view/clear the per-person voice profile (VP / VP FORGET)
    RELATIONSHIP_MEMORY = "RELATIONSHIP_MEMORY"  # view/clear saved facts about a counterparty (RM / RM FORGET)
    TRIAGE = "TRIAGE"  # read-only: rank a bundled forward of several threads into a digest
    HELP = "HELP"  # reply with the command-code reference (read-only)
    CLARIFY = "CLARIFY"
    DEFERRED = "DEFERRED"  # recognized code, but not actionable in this version


class ResultKind(str, Enum):
    """What Nora produced for a request, driving receipt composition (U6)."""

    DRAFT = "DRAFT"
    SUMMARY = "SUMMARY"
    ANALYSIS = "ANALYSIS"
    REPORT = "REPORT"
    CLARIFY = "CLARIFY"
    LOST_THREAD = "LOST_THREAD"
    DEFERRED = "DEFERRED"
    PROPOSAL = "PROPOSAL"            # a consent-gated action proposed, awaiting confirm
    ACTION_SENT = "ACTION_SENT"      # a confirmed action was executed (sent)
    ACTION_DECLINED = "ACTION_DECLINED"  # the CEO declined a proposed action


class Attachment(BaseModel):
    """Attachment metadata + optional decoded content. ``content`` is transient
    (used for extraction); it is never persisted onto a stored thread."""

    filename: str
    content_type: str | None = None
    size: int | None = None        # original byte size (Postmark ContentLength)
    content: bytes | None = None   # decoded bytes; None when omitted/too-large/absent


class OutboundAttachment(BaseModel):
    """A file Nora attaches to an outgoing reply (e.g. a generated report)."""

    filename: str
    content: bytes
    content_type: str = "application/octet-stream"


class ExtractedAttachment(BaseModel):
    """Local text-extraction result for one attachment (text only — safe to persist)."""

    filename: str
    status: str                    # "read" | "unreadable"
    text: str = ""
    reason: str | None = None      # unreadable: unsupported_type|too_large|no_text_found|extract_failed|budget_exhausted
    truncated: bool = False


class ParsedThread(BaseModel):
    """The forwarded original thread, extracted from the CEO's forward."""

    original_sender: str | None = None
    recipients: list[str] = Field(default_factory=list)
    subject: str | None = None
    body: str = ""
    attachments: list[Attachment] = Field(default_factory=list)
    attachment_extracts: list[ExtractedAttachment] = Field(default_factory=list)


class InboundEmail(BaseModel):
    """A normalized inbound message from the email provider (post-verification)."""

    event_id: str
    secret_address_local: str  # local-part the CEO forwarded to (e.g. "nora-abc123")
    from_address: str  # wrapper From of the forwarding CEO
    dkim_verified: bool = False
    subject: str = ""
    body: str = ""
    correlation_token: str | None = None  # recovered from a plus-addressed reply
    attachments: list[Attachment] = Field(default_factory=list)


class ParsedRequest(BaseModel):
    """The CEO's instruction plus the thread it applies to (U3 output)."""

    command: Command = Command.NONE
    instruction_text: str | None = None  # captured NL request / bracketed notes
    thread: ParsedThread = Field(default_factory=ParsedThread)
    recipient_uncertain: bool = False
    correlation_token: str | None = None
    # NB command-code vocabulary (app/codes.py). action_code is the canonical
    # primary action (e.g. "RE", "AC", "PR"); deferred_code is set when a
    # recognized action isn't actionable in the drafts-only prototype.
    action_code: str | None = None
    deferred_code: str | None = None
    modifier_codes: list[str] = Field(default_factory=list)
    target_codes: list[str] = Field(default_factory=list)
    raw_code_line: str | None = None  # echoed to the LLM so it applies refinements
    # The CEO's own prior in-thread messages, extracted as a voice exemplar (U1).
    # "" when the thread has none. Content — must never be logged/emitted (see
    # app.metrics _FORBIDDEN_KEYS); it rides only into the drafting prompt.
    voice_exemplar: str = ""
    # Approval reaction parsed from the authored region ("confirm"/"decline"/None) —
    # the consent-gate signal for a meeting proposal (0.2.0). Never from quoted text.
    approval_reaction: str | None = None
    # Metadata-only relationship breadcrumb recalled from cross-thread memory (Layer A);
    # rides into the drafting prompt as fenced DATA. R1-only; "" / None when off.
    relationship_context: str | None = None
    # Per-person voice profile (Layer B), recalled on the R1 path; a short style note in the
    # drafting prompt so drafts start in the CEO's voice. None = off.
    voice_profile: str | None = None
    # Triage (TR): the bundled forward split into per-thread items, fed to the ranking prompt.
    triage_items: list[str] = Field(default_factory=list)
    # Triage v2: a per-item sender-familiarity hint (parallel to triage_items; "" when no sender
    # or memory off), rendered on the thread header so the ranking can weigh recurring senders.
    triage_relationship_hints: list[str] = Field(default_factory=list)
    # Text of a referenced company file (files-domain access, Layer A), read on demand
    # from the connected store; rides into the drafting prompt as fenced DATA. None = none.
    file_context: str | None = None
    # Layer C v2 (auto-learn): when True, the drafting prompt/schema invite optional
    # `derived_facts` so Nora learns durable relationship facts. Set by the pipeline on the
    # R1 draft path only when NORA_RELATIONSHIP_AUTOLEARN is on; never off the R1 path.
    autolearn: bool = False


class NoraResult(BaseModel):
    """What Nora produced, consumed by receipt composition (U6)."""

    kind: ResultKind
    text: str = ""  # the draft, summary, or clarifying question
    interpretation_readback: str = ""
    uncertainty: str | None = None
    recipient: str | None = None
    recipient_uncertain: bool = False
    # Layer C v2: durable relationship facts derived from the thread this turn. Carries the
    # model's raw output through result mapping; the pipeline overwrites it with only the
    # NEWLY-learned facts before the receipt surfaces them. [] when autolearn is off.
    derived_facts: list[str] = []
