from __future__ import annotations

from app.drafting.result import fields_valid, result_from_fields
from app.models import Intent, ParsedRequest, ParsedThread, ResultKind


def _req(sender="sarah@acme.com", autolearn=False):
    return ParsedRequest(thread=ParsedThread(original_sender=sender), autolearn=autolearn)


def test_fields_valid() -> None:
    assert fields_valid({"draft_or_summary": "hi", "interpretation_readback": "draft"})
    assert not fields_valid({"draft_or_summary": " ", "interpretation_readback": "x"})
    assert not fields_valid({"draft_or_summary": "x"})


def test_result_from_fields_maps_kind_and_recipient() -> None:
    data = {"draft_or_summary": "Body", "interpretation_readback": "Draft a reply.",
            "recipient": "sarah@acme.com"}
    r = result_from_fields(data, _req(), Intent.DRAFT)
    assert r.kind is ResultKind.DRAFT and r.text == "Body"
    assert r.recipient == "sarah@acme.com" and r.recipient_uncertain is False


def test_result_from_fields_flags_anomalous_recipient() -> None:
    data = {"draft_or_summary": "B", "interpretation_readback": "I", "recipient": "evil@x.com"}
    r = result_from_fields(data, _req("sarah@acme.com"), Intent.DRAFT)
    assert r.recipient_uncertain is True
    assert "not a participant" in (r.uncertainty or "")


def test_result_from_fields_parses_derived_facts_when_autolearn() -> None:
    data = {
        "draft_or_summary": "B", "interpretation_readback": "I",
        "recipient": "sarah@acme.com",
        "derived_facts": ["prefers mornings", "  ", "x" * 500, "is the lead", "a 4th fact"],
    }
    r = result_from_fields(data, _req(autolearn=True), Intent.DRAFT)
    assert r.derived_facts == ["prefers mornings", "x" * 160, "is the lead"]  # empties dropped, ≤3, trimmed


def test_result_from_fields_ignores_derived_facts_when_autolearn_off() -> None:
    data = {"draft_or_summary": "B", "interpretation_readback": "I",
            "recipient": "sarah@acme.com", "derived_facts": ["prefers mornings"]}
    r = result_from_fields(data, _req(autolearn=False), Intent.DRAFT)
    assert r.derived_facts == []


def test_result_from_fields_tolerates_garbage_derived_facts() -> None:
    data = {"draft_or_summary": "B", "interpretation_readback": "I",
            "recipient": "sarah@acme.com", "derived_facts": "not a list"}
    r = result_from_fields(data, _req(autolearn=True), Intent.DRAFT)
    assert r.derived_facts == []
