"""Deterministic intent routing (U4).

Maps a parsed request to an intent with **no model call** (KTD8). Command codes
route directly via the canonical action map (`app/codes.py`); research (FI/I) routes
to RESEARCH (a pluggable backend that declines when unconfigured); any future
recognized-but-non-actionable code routes to DEFERRED; otherwise
natural-language keywords route, and anything unrecognized becomes a single
clarifying question. The model call (U5) only happens for the producing intents.
"""

from __future__ import annotations

from app.models import Command, Intent, ParsedRequest

_ACTION_TO_INTENT = {
    "RE": Intent.DRAFT,
    "WIYOW": Intent.DRAFT,
    "SC": Intent.SUMMARIZE,
    "S": Intent.SUMMARIZE,
    "AC": Intent.ANALYZE,
    "AR": Intent.ANALYZE,
    "PR": Intent.REPORT,
    "BM": Intent.PROPOSE_MEETING,
    "SM": Intent.PROPOSE_MEETING,
    "FI": Intent.RESEARCH,
    "I": Intent.RESEARCH,
    "SH": Intent.PROPOSE_SHARE,
    "VP": Intent.VOICE_PROFILE,
    "RM": Intent.RELATIONSHIP_MEMORY,
    "TR": Intent.TRIAGE,
    "HELP": Intent.HELP,
}

_SUMMARIZE_HINTS = ("summari", "summary", "recap", "tldr", "tl;dr", "brief me", "what's this")
_ANALYZE_HINTS = ("analyz", "analyse", "critique", "assess", "evaluate", "your take")
_REPORT_HINTS = ("report on", "write a report", "prepare a report", "write up", "write-up")
_DRAFT_HINTS = (
    "reply",
    "respond",
    "draft",
    "decline",
    "accept",
    "answer",
    "write back",
    "tell them",
    "let them know",
    "follow up",
)

_DEFAULT_QUESTION = (
    "Did you want me to reply (RE), summarize (SC/S), analyze (AC), or write a "
    "report (PR)? Reply with a code or plain English."
)


def route(req: ParsedRequest) -> tuple[Intent, str | None]:
    """Return the routing intent and, for CLARIFY, the question to ask."""
    # Recognized code that this version cannot act on (send/book/research).
    if req.deferred_code:
        return Intent.DEFERRED, None
    # Canonical action code.
    if req.action_code and req.action_code in _ACTION_TO_INTENT:
        intent = _ACTION_TO_INTENT[req.action_code]
        # SM.A / BM.A: the "A" modifier on a meeting-propose means "calendar hold"
        # instead of an email proposal (deterministic, no LLM, consent-gated).
        if intent is Intent.PROPOSE_MEETING and "A" in (req.modifier_codes or []):
            return Intent.PROPOSE_CALENDAR, None
        return intent, None
    # Legacy command fallback (callers that set only `command`).
    if req.command is Command.WIYOW:
        return Intent.DRAFT, None
    if req.command is Command.S:
        return Intent.SUMMARIZE, None

    text = (req.instruction_text or "").lower().strip()
    if not text:
        return Intent.CLARIFY, _DEFAULT_QUESTION
    if any(h in text for h in _SUMMARIZE_HINTS):
        return Intent.SUMMARIZE, None
    if any(h in text for h in _REPORT_HINTS):
        return Intent.REPORT, None
    if any(h in text for h in _ANALYZE_HINTS):
        return Intent.ANALYZE, None
    if any(h in text for h in _DRAFT_HINTS):
        return Intent.DRAFT, None
    return Intent.CLARIFY, _DEFAULT_QUESTION
