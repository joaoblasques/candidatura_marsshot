from __future__ import annotations

import json

import pytest

from app.drafting.ollama import OllamaDraftingService
from app.drafting.result import DraftingError
from app.models import Intent, ParsedRequest, ParsedThread, ResultKind


class _FakeResp:
    def __init__(self, payload): self._p = payload
    def raise_for_status(self): pass
    def json(self): return self._p


class _FakeClient:
    def __init__(self, contents): self._contents = list(contents); self.calls = []  # noqa: E702
    def post(self, url, json=None):  # noqa: A002
        self.calls.append((url, json))
        return _FakeResp({"message": {"content": self._contents.pop(0)}})


def _req():
    return ParsedRequest(thread=ParsedThread(original_sender="sarah@acme.com"))


def test_ollama_maps_structured_json_to_result() -> None:
    body = json.dumps({"draft_or_summary": "Hi Sarah", "interpretation_readback": "Draft a reply.",
                       "recipient": "sarah@acme.com"})
    client = _FakeClient([body])
    svc = OllamaDraftingService("http://localhost:11434", "qwen3:8b", client=client)
    r = svc.produce(_req(), Intent.DRAFT)
    assert r.kind is ResultKind.DRAFT and r.text == "Hi Sarah"
    assert client.calls[0][0].endswith("/api/chat")
    assert client.calls[0][1]["model"] == "qwen3:8b"
    assert client.calls[0][1]["stream"] is False
    # think=False suppresses qwen3's <think> block, which would otherwise break json.loads;
    # format pins the structured-output schema. Both are load-bearing — guard them.
    assert client.calls[0][1]["think"] is False
    assert "format" in client.calls[0][1]
    # Lean local prompt: the heavy command-code @-grammar reference must NOT be sent —
    # an 8B loops it into the recipient field (observed). And the schema must carry the
    # field descriptions (esp. draft_or_summary) so the model returns content, not a label.
    system_msg = client.calls[0][1]["messages"][0]["content"]
    assert "NORA COMMAND CODES" not in system_msg
    assert client.calls[0][1]["format"]["properties"]["draft_or_summary"].get("description")


def test_ollama_strips_trailing_name_placeholder_on_draft() -> None:
    body = json.dumps({
        "draft_or_summary": "Hi Sarah,\n\nThanks for the note.\n\nBest,\n\nCEO",
        "interpretation_readback": "Draft a reply.", "recipient": "sarah@acme.com",
    })
    svc = OllamaDraftingService("http://localhost:11434", "qwen3:8b", client=_FakeClient([body]))
    r = svc.produce(_req(), Intent.DRAFT)
    assert r.text == "Hi Sarah,\n\nThanks for the note.\n\nBest,"   # "CEO" line removed, sign-off kept
    # Bracketed placeholder is stripped too.
    body2 = json.dumps({
        "draft_or_summary": "Hi,\n\nDone.\n\n[Your Name]",
        "interpretation_readback": "Draft a reply.", "recipient": "sarah@acme.com",
    })
    r2 = OllamaDraftingService("http://localhost:11434", "q", client=_FakeClient([body2])).produce(
        _req(), Intent.DRAFT)
    assert r2.text == "Hi,\n\nDone."


def test_ollama_does_not_strip_legitimate_final_line() -> None:
    # A real closing sentence must survive — only a lone placeholder is stripped.
    body = json.dumps({
        "draft_or_summary": "Hi Sarah,\n\nThe CEO will join the call.",
        "interpretation_readback": "Draft a reply.", "recipient": "sarah@acme.com",
    })
    r = OllamaDraftingService("http://localhost:11434", "q", client=_FakeClient([body])).produce(
        _req(), Intent.DRAFT)
    assert r.text == "Hi Sarah,\n\nThe CEO will join the call."


def test_ollama_retries_then_raises_on_malformed() -> None:
    client = _FakeClient(["not json", "{}"])
    svc = OllamaDraftingService("http://localhost:11434", "qwen3:8b", client=client)
    with pytest.raises(DraftingError):
        svc.produce(_req(), Intent.DRAFT)
    assert len(client.calls) == 2
