"""Shared drafting helpers: validate structured fields and map them to a NoraResult,
including the KTD9 recipient-anomaly check. Used by every drafting backend (Claude,
Ollama) so the mapping + injection guard can't diverge by provider."""

from __future__ import annotations

from typing import Protocol

from app.models import Intent, NoraResult, ParsedRequest, ResultKind

_INTENT_KIND = {
    Intent.DRAFT: ResultKind.DRAFT,
    Intent.SUMMARIZE: ResultKind.SUMMARY,
    Intent.ANALYZE: ResultKind.ANALYSIS,
    Intent.REPORT: ResultKind.REPORT,
    Intent.PROPOSE_MEETING: ResultKind.PROPOSAL,
}


class DraftingError(RuntimeError):
    """Raised when a backend could not produce valid structured output."""


class Drafting(Protocol):
    def produce(self, req: ParsedRequest, intent: Intent) -> NoraResult: ...


def fields_valid(data: dict) -> bool:
    return bool(str(data.get("draft_or_summary", "")).strip()) and bool(
        str(data.get("interpretation_readback", "")).strip()
    )


def _is_known_participant(recipient: str, req: ParsedRequest) -> bool:
    if not req.thread.original_sender:
        return False
    return recipient.lower() == req.thread.original_sender.lower()


def _parse_derived_facts(raw) -> list[str]:
    """Defensive parse of the model's optional `derived_facts` (Layer C v2): coerce to a list
    of terse non-empty strings, at most 3, each trimmed to 160 chars. Anything else → []."""
    if not isinstance(raw, list):
        return []
    facts = []
    for item in raw:
        fact = str(item).strip()[:160]
        if fact:
            facts.append(fact)
    return facts[:3]


def result_from_fields(data: dict, req: ParsedRequest, intent: Intent) -> NoraResult:
    kind = _INTENT_KIND.get(intent, ResultKind.SUMMARY)
    recipient = data.get("recipient") or req.thread.original_sender
    uncertainty = data.get("uncertainty") or None
    recipient_uncertain = req.recipient_uncertain
    if recipient and not _is_known_participant(recipient, req):
        recipient_uncertain = True
        note = f"Proposed recipient {recipient} is not a participant in the thread — verify."
        uncertainty = f"{uncertainty} {note}".strip() if uncertainty else note
    return NoraResult(
        kind=kind,
        text=str(data["draft_or_summary"]).strip(),
        interpretation_readback=str(data["interpretation_readback"]).strip(),
        uncertainty=uncertainty,
        recipient=recipient,
        recipient_uncertain=recipient_uncertain,
        derived_facts=_parse_derived_facts(data.get("derived_facts")) if req.autolearn else [],
    )
