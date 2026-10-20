"""U4: deterministic intent routing (no model call)."""

from __future__ import annotations

from app.interpreter import route
from app.models import Command, Intent, ParsedRequest


def _req(command: Command = Command.NONE, instruction: str | None = None) -> ParsedRequest:
    return ParsedRequest(command=command, instruction_text=instruction)


def test_code_wiyow_routes_to_draft() -> None:
    assert route(_req(command=Command.WIYOW)) == (Intent.DRAFT, None)


def test_code_s_routes_to_summarize() -> None:
    assert route(_req(command=Command.S)) == (Intent.SUMMARIZE, None)


def test_nl_summarize() -> None:
    intent, q = route(_req(instruction="can you summarize this thread"))
    assert intent is Intent.SUMMARIZE and q is None


def test_nl_draft() -> None:
    intent, q = route(_req(instruction="reply declining politely"))
    assert intent is Intent.DRAFT and q is None


def test_unrecognized_nl_clarifies() -> None:
    intent, q = route(_req(instruction="handle this"))
    assert intent is Intent.CLARIFY and q is not None


def test_bare_forward_no_instruction_clarifies() -> None:
    intent, q = route(_req())
    assert intent is Intent.CLARIFY and q


def test_routing_makes_no_model_call(monkeypatch) -> None:
    # If the interpreter ever imported/used an Anthropic client, this guards it:
    # routing must complete with no network/model dependency available.
    import app.interpreter as interp

    assert not hasattr(interp, "anthropic")  # no client imported into the module
    # And routing still works with codes and NL.
    assert route(_req(command=Command.WIYOW))[0] is Intent.DRAFT
    assert route(_req(instruction="recap please"))[0] is Intent.SUMMARIZE


def test_tr_routes_to_triage():
    from app.interpreter import route
    from app.models import Intent, ParsedRequest
    req = ParsedRequest(action_code="TR", raw_code_line="NB-TR")
    intent, _ = route(req)
    assert intent is Intent.TRIAGE
