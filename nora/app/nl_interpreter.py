"""Plain-language interpreter (the NL front-end onto the command pipeline).

Turns free-form English into the SAME ParsedRequest fields a code produces, so route()/producing/
consent run unchanged. Default NullInterpreter = no-op (today's keyword fallback in route() then
handles NL). The LLM backend (R1/cloud only) lives here; KL/R3 keep the Null/keyword path."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from app.models import ParsedRequest

# Interpreter intent -> canonical action code (the same codes route() already maps).
_INTENT_TO_CODE = {
    "reply": "RE",
    "summarize": "SC",
    "analyze": "AC",
    "report": "PR",
    "propose_meeting": "SM",
    "share_file": "SH",
    "triage": "TR",
    "research": "FI",
    "help": "HELP",
}
_TONE_MOD = {"formal": "F", "informal": "I"}
_LENGTH_MOD = {"short": "SHORT", "long": "LONG"}


@dataclass
class InterpretedRequest:
    intent: str | None = None          # an _INTENT_TO_CODE key, "unclear", or None
    modifiers: list[str] = field(default_factory=list)  # canonical refinements (MN/F/I/SHORT/LONG)
    target: str | None = None          # an explicitly-named recipient address
    notes: str | None = None           # extra free-text instruction for drafting
    clarify: str | None = None         # a question to ask when intent is unclear


class Interpreter(Protocol):
    def interpret(self, text: str) -> InterpretedRequest: ...


class NullInterpreter:
    """Default: no interpretation — route()'s keyword fallback handles NL (today's behavior)."""

    def interpret(self, text: str) -> InterpretedRequest:
        return InterpretedRequest()


def apply_interpretation(req: ParsedRequest, ir: InterpretedRequest) -> ParsedRequest:
    """Map an InterpretedRequest onto the ParsedRequest, exactly as a code would have populated it."""
    code = _INTENT_TO_CODE.get(ir.intent or "")
    if code:
        req.action_code = code
    if ir.modifiers:
        req.modifier_codes = list(req.modifier_codes or []) + [m for m in ir.modifiers if m]
    if ir.target:
        req.target_codes = [f"@{ir.target.strip().lower()}"]
    if ir.notes:
        req.instruction_text = ir.notes
    return req


# --- LLM backend (R1 / cloud only) ----------------------------------------------

_INTERPRETER_TOOL = {
    "name": "interpret_request",
    "description": "Classify the CEO's plain-language request to their email assistant.",
    "input_schema": {
        "type": "object",
        "properties": {
            "intent": {
                "type": "string",
                "enum": [
                    "reply", "summarize", "analyze", "report", "propose_meeting",
                    "share_file", "triage", "research", "help", "unclear",
                ],
                "description": (
                    "What the CEO wants. 'propose_meeting'/'share_file' are consent-gated proposals "
                    "(still approved before anything sends). Use 'unclear' if you cannot tell — never "
                    "guess a consent action."
                ),
            },
            "tone": {"type": "string", "enum": ["neutral", "formal", "informal"]},
            "length": {"type": "string", "enum": ["default", "short", "long"]},
            "in_my_name": {"type": "boolean", "description": "true if they want it written as themselves."},
            "target": {"type": ["string", "null"], "description": "An explicitly named recipient email, else null."},
            "notes": {"type": ["string", "null"], "description": "Any extra instruction to pass through to drafting."},
            "clarify": {"type": ["string", "null"], "description": "If intent is 'unclear', a short question to ask."},
        },
        "required": ["intent"],
    },
}

_INTERPRETER_SYSTEM = (
    "You route an email assistant. Given the CEO's plain-language instruction, call interpret_request "
    "with the single best intent and any tone/length/in_my_name/target/notes you can infer. Do not "
    "guess a consent action (propose_meeting/share_file) unless the CEO clearly asks to set up a "
    "meeting or share a file; otherwise prefer reply/summarize/analyze/report, or 'unclear'."
)


# JSON-mode system prompt for OpenAI-compatible models (no Anthropic tool-use): same intent
# enum + fields as _INTERPRETER_TOOL, but asked for as a single JSON object.
_INTERPRETER_JSON_SYSTEM = (
    _INTERPRETER_SYSTEM
    + " Respond with ONLY a single JSON object with keys: intent (one of "
    "reply|summarize|analyze|report|propose_meeting|share_file|triage|research|help|unclear), "
    "tone (neutral|formal|informal), length (default|short|long), in_my_name (boolean), "
    "target (an email string or null), notes (string or null), clarify (string or null)."
)

_UNCLEAR_FALLBACK = InterpretedRequest(
    intent="unclear", clarify="Could you say what you'd like me to do?"
)


def _extract_tool_input(response: object) -> dict | None:
    content = getattr(response, "content", None) or []
    for block in content:
        if getattr(block, "type", None) == "tool_use" and getattr(block, "name", "") == "interpret_request":
            data = getattr(block, "input", None)
            if isinstance(data, dict):
                return data
    return None


def _interpreted_from_data(data: object) -> InterpretedRequest:
    """Map a model's classification dict onto an InterpretedRequest. Shared by every LLM
    backend so they can't drift; an invalid/missing dict yields the fail-safe clarify."""
    if not isinstance(data, dict):
        return _UNCLEAR_FALLBACK
    mods: list[str] = []
    if data.get("in_my_name"):
        mods.append("MN")
    if data.get("tone") in _TONE_MOD:
        mods.append(_TONE_MOD[data["tone"]])
    if data.get("length") in _LENGTH_MOD:
        mods.append(_LENGTH_MOD[data["length"]])
    return InterpretedRequest(
        intent=str(data.get("intent") or "unclear"),
        modifiers=mods,
        target=(data.get("target") or None),
        notes=(data.get("notes") or None),
        clarify=(data.get("clarify") or None),
    )


class LLMInterpreter:
    """R1/cloud interpreter: one forced tool call -> InterpretedRequest. Client injected (testable)."""

    def __init__(self, client: Any, model: str) -> None:
        self._client = client
        self._model = model

    def interpret(self, text: str) -> InterpretedRequest:
        try:
            resp = self._client.messages.create(
                model=self._model,
                max_tokens=300,
                system=_INTERPRETER_SYSTEM,
                tools=[_INTERPRETER_TOOL],
                tool_choice={"type": "tool", "name": "interpret_request"},
                messages=[{"role": "user", "content": text or ""}],
            )
            data = _extract_tool_input(resp)
        except Exception:  # noqa: BLE001 — any API/parse failure falls back to a clarify (fail-safe)
            data = None
        return _interpreted_from_data(data)


class OpenRouterInterpreter:
    """OpenAI-compatible (OpenRouter) interpreter: JSON-mode classification -> InterpretedRequest.
    Mirror of LLMInterpreter for the non-Anthropic provider; same fail-safe clarify on error."""

    def __init__(self, base_url: str, api_key: str, model: str, timeout: float = 30.0, client: Any = None) -> None:
        import httpx

        self._base_url = base_url
        self._api_key = api_key
        self._model = model
        self._client = client or httpx.Client(timeout=timeout)

    def interpret(self, text: str) -> InterpretedRequest:
        from app.drafting.json_backend import parse_json_content
        from app.drafting.openrouter import chat_completion

        try:
            content = chat_completion(
                self._client, self._base_url, self._api_key, self._model,
                _INTERPRETER_JSON_SYSTEM, text or "", max_tokens=300,
            )
            data = parse_json_content(content)
        except Exception:  # noqa: BLE001 — any API/parse failure falls back to a clarify (fail-safe)
            data = None
        return _interpreted_from_data(data)
