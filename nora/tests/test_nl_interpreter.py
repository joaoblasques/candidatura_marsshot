from app.models import ParsedRequest
from app.nl_interpreter import InterpretedRequest, NullInterpreter, apply_interpretation


def test_null_interpreter_returns_empty():
    ir = NullInterpreter().interpret("reply warmly")
    assert ir.intent is None and ir.modifiers == [] and ir.clarify is None


def test_apply_maps_intent_modifiers_target_notes():
    ir = InterpretedRequest(intent="reply", modifiers=["I", "SHORT"], target="sarah@acme.com",
                            notes="mention Tuesday", clarify=None)
    out = apply_interpretation(ParsedRequest(), ir)
    assert out.action_code == "RE"
    assert "I" in out.modifier_codes and "SHORT" in out.modifier_codes
    assert out.target_codes == ["@sarah@acme.com"]
    assert out.instruction_text == "mention Tuesday"


def test_apply_unclear_leaves_action_code_none():
    out = apply_interpretation(ParsedRequest(), InterpretedRequest(
        intent="unclear", clarify="What should I do?"))
    assert out.action_code is None


class _FakeBlock:
    def __init__(self, data):
        self.type = "tool_use"
        self.name = "interpret_request"
        self.input = data


class _FakeResp:
    def __init__(self, data):
        self.content = [_FakeBlock(data)]


class _FakeMessages:
    def __init__(self, data):
        self._data = data

    def create(self, **kw):
        return _FakeResp(self._data)


class _FakeClient:
    def __init__(self, data):
        self.messages = _FakeMessages(data)


def test_llm_interpreter_maps_tool_output():
    from app.nl_interpreter import LLMInterpreter
    client = _FakeClient({"intent": "reply", "tone": "informal", "length": "short",
                          "in_my_name": True, "target": "sarah@acme.com", "notes": "mention Tue"})
    ir = LLMInterpreter(client, model="m").interpret("reply to sarah informally, short, in my name")
    assert ir.intent == "reply" and ir.target == "sarah@acme.com" and ir.notes == "mention Tue"
    assert "I" in ir.modifiers and "SHORT" in ir.modifiers and "MN" in ir.modifiers


def test_llm_interpreter_unclear_on_malformed():
    from app.nl_interpreter import LLMInterpreter
    ir = LLMInterpreter(_FakeClient("not a dict"), model="m").interpret("???")
    assert ir.intent == "unclear" and ir.clarify


class _HttpResp:
    def __init__(self, content): self._content = content
    def raise_for_status(self): pass
    def json(self): return {"choices": [{"message": {"content": self._content}}]}


class _HttpClient:
    def __init__(self, content): self._content = content; self.calls = []  # noqa: E702
    def post(self, url, headers=None, json=None):  # noqa: A002
        self.calls.append({"url": url, "headers": headers, "json": json})
        return _HttpResp(self._content)


def test_openrouter_interpreter_maps_json_output():
    import json

    from app.nl_interpreter import OpenRouterInterpreter
    payload = json.dumps({"intent": "summarize", "length": "short", "in_my_name": True})
    http = _HttpClient(payload)
    ir = OpenRouterInterpreter("https://openrouter.ai/api/v1", "sk-or", "m", client=http).interpret(
        "summarize this briefly in my name")
    assert ir.intent == "summarize" and "SHORT" in ir.modifiers and "MN" in ir.modifiers
    assert http.calls[0]["url"].endswith("/chat/completions")
    assert http.calls[0]["headers"]["Authorization"] == "Bearer sk-or"


def test_openrouter_interpreter_unclear_on_malformed():
    from app.nl_interpreter import OpenRouterInterpreter
    ir = OpenRouterInterpreter("https://openrouter.ai/api/v1", "sk-or", "m",
                               client=_HttpClient("not json")).interpret("???")
    assert ir.intent == "unclear" and ir.clarify
