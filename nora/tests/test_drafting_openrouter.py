from __future__ import annotations

import json

import pytest

from app.drafting.openrouter import OpenRouterDraftingService
from app.drafting.result import DraftingError
from app.models import Intent, ParsedRequest, ParsedThread, ResultKind


class _FakeResp:
    def __init__(self, content): self._content = content
    def raise_for_status(self): pass
    def json(self): return {"choices": [{"message": {"content": self._content}}]}


class _FakeClient:
    def __init__(self, contents): self._contents = list(contents); self.calls = []  # noqa: E702
    def post(self, url, headers=None, json=None):  # noqa: A002
        self.calls.append({"url": url, "headers": headers, "json": json})
        return _FakeResp(self._contents.pop(0))


def _req():
    return ParsedRequest(thread=ParsedThread(original_sender="sarah@acme.com"))


def _svc(contents):
    return OpenRouterDraftingService(
        "https://openrouter.ai/api/v1", "sk-or-test", "meta-llama/llama-3.3-70b-instruct:free",
        client=_FakeClient(contents),
    )


def test_openrouter_maps_structured_json_to_result() -> None:
    body = json.dumps({"draft_or_summary": "Hi Sarah", "interpretation_readback": "Draft a reply.",
                       "recipient": "sarah@acme.com"})
    svc = _svc([body])
    r = svc.produce(_req(), Intent.DRAFT)
    assert r.kind is ResultKind.DRAFT and r.text == "Hi Sarah"
    call = svc._client.calls[0]  # type: ignore[attr-defined]
    assert call["url"].endswith("/chat/completions")
    assert call["headers"]["Authorization"] == "Bearer sk-or-test"
    assert call["json"]["model"] == "meta-llama/llama-3.3-70b-instruct:free"
    # OpenAI-format messages; structured-output requested via json_schema.
    assert call["json"]["messages"][0]["role"] == "system"
    assert call["json"]["response_format"]["type"] == "json_schema"
    # Lean prompt: the heavy @-code grammar must not be sent (open models loop on it).
    assert "NORA COMMAND CODES" not in call["json"]["messages"][0]["content"]


def test_openrouter_tolerates_markdown_fenced_json() -> None:
    fenced = "```json\n" + json.dumps({
        "draft_or_summary": "Summary text.", "interpretation_readback": "Summarize.",
        "recipient": "sarah@acme.com"}) + "\n```"
    r = _svc([fenced]).produce(_req(), Intent.SUMMARIZE)
    assert r.kind is ResultKind.SUMMARY and "Summary text." in r.text


def test_openrouter_strips_trailing_name_placeholder_on_draft() -> None:
    body = json.dumps({
        "draft_or_summary": "Hi Sarah,\n\nThanks.\n\nBest,\n\n[Your Name]",
        "interpretation_readback": "Draft a reply.", "recipient": "sarah@acme.com",
    })
    r = _svc([body]).produce(_req(), Intent.DRAFT)
    assert r.text == "Hi Sarah,\n\nThanks.\n\nBest,"


def test_openrouter_retries_then_raises_on_malformed() -> None:
    svc = _svc(["not json at all", "{}"])
    with pytest.raises(DraftingError):
        svc.produce(_req(), Intent.DRAFT)
    calls = svc._client.calls  # type: ignore[attr-defined]
    assert len(calls) == 2
    # First call requests strict json_schema; the retry drops response_format so a model
    # that can't do structured output (OpenRouter errors on it) still gets a plain attempt.
    assert calls[0]["json"]["response_format"]["type"] == "json_schema"
    assert "response_format" not in calls[1]["json"]


def test_openrouter_retries_on_transient_429() -> None:
    import json as _json

    good = _json.dumps({"draft_or_summary": "Hi", "interpretation_readback": "Reply.",
                        "recipient": "sarah@acme.com"})

    class _RateLimitedResp:
        status_code = 429
        headers = {"Retry-After": "0"}  # instant backoff in the test
        def raise_for_status(self): raise AssertionError("should have retried, not raised")
        def json(self): return {}

    class _OkResp:
        status_code = 200
        headers: dict = {}
        def raise_for_status(self): pass
        def json(self): return {"choices": [{"message": {"content": good}}]}

    class _FlakyClient:
        def __init__(self): self.calls = 0; self._seq = [_RateLimitedResp(), _OkResp()]  # noqa: E702
        def post(self, url, headers=None, json=None):  # noqa: A002
            self.calls += 1
            return self._seq.pop(0)

    svc = OpenRouterDraftingService("https://openrouter.ai/api/v1", "k", "m", client=_FlakyClient())
    r = svc.produce(_req(), Intent.DRAFT)
    assert r.text == "Hi" and svc._client.calls == 2  # type: ignore[attr-defined]


def test_openrouter_schema_unsupported_falls_back_to_plain(monkeypatch) -> None:
    # Simulate a provider that 400s on json_schema: first attempt raises, plain retry succeeds.
    import json as _json

    good = _json.dumps({"draft_or_summary": "Hi", "interpretation_readback": "Reply.",
                        "recipient": "sarah@acme.com"})

    class _PickyClient:
        def __init__(self): self.calls = []
        def post(self, url, headers=None, json=None):  # noqa: A002
            self.calls.append(json)
            if "response_format" in json:
                raise RuntimeError("provider does not support response_format")
            return _FakeResp(good)

    svc = OpenRouterDraftingService("https://openrouter.ai/api/v1", "k", "m", client=_PickyClient())
    r = svc.produce(_req(), Intent.DRAFT)
    assert r.text == "Hi" and len(svc._client.calls) == 2  # type: ignore[attr-defined]
