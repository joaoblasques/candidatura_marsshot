"""Resend outbound send client (mocked httpx transport, no network)."""

from __future__ import annotations

import json

import httpx
import pytest

from app.email.resend_send import ResendSendClient


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_resend_posts_expected_payload_with_per_person_sender() -> None:
    cap: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        cap["url"] = str(request.url)
        cap["auth"] = request.headers.get("authorization")
        cap["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "abc"})

    c = ResendSendClient("re_key", "nora@nora-bennett.com", client=_client(handler))
    c.send(
        to="ceo@example.com",
        subject="Nora · draft reply",
        body="Here's a draft.",
        reply_to="nora+corr99@nora-bennett.com",
        sender="democeo@nora-bennett.com",
    )
    assert cap["url"] == "https://api.resend.com/emails"
    assert cap["auth"] == "Bearer re_key"
    assert cap["body"]["from"] == "democeo@nora-bennett.com"  # per-person override
    assert cap["body"]["to"] == ["ceo@example.com"]
    assert cap["body"]["reply_to"] == "nora+corr99@nora-bennett.com"
    assert cap["body"]["text"] == "Here's a draft."


def test_resend_falls_back_to_default_sender_and_omits_reply_to() -> None:
    cap: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        cap["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "x"})

    c = ResendSendClient("k", "nora@nora-bennett.com", client=_client(handler))
    c.send(to="ceo@example.com", subject="s", body="b", reply_to=None)
    assert cap["body"]["from"] == "nora@nora-bennett.com"
    assert "reply_to" not in cap["body"]


def test_resend_raises_on_non_2xx() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"message": "Invalid"})

    c = ResendSendClient("k", "nora@x", client=_client(handler))
    with pytest.raises(httpx.HTTPStatusError):
        c.send(to="x@y.com", subject="s", body="b", reply_to=None)


def test_resend_raises_on_transport_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    c = ResendSendClient("k", "nora@x", client=_client(handler))
    with pytest.raises(httpx.ConnectError):
        c.send(to="x@y.com", subject="s", body="b", reply_to=None)


def test_resend_includes_base64_attachments() -> None:
    import base64

    from app.models import OutboundAttachment

    cap: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        cap["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "abc"})

    c = ResendSendClient("re_key", "nora@nora-bennett.com", client=_client(handler))
    c.send(
        to="ceo@example.com", subject="Nora · report", body="see attached",
        reply_to=None,
        attachments=[OutboundAttachment(filename="Nora-report.docx", content=b"PKxx",
                                        content_type="application/x")],
    )
    [a] = cap["body"]["attachments"]
    assert a["filename"] == "Nora-report.docx"
    assert base64.b64decode(a["content"]) == b"PKxx"
