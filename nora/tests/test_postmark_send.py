"""U3: Postmark outbound send client (mocked httpx transport, no network)."""

from __future__ import annotations

import json

import httpx
import pytest

from app.email.postmark_send import PostmarkSendClient


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_send_posts_expected_postmark_payload() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["token"] = request.headers.get("X-Postmark-Server-Token")
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"ErrorCode": 0, "Message": "OK"})

    pm = PostmarkSendClient("server-token", "nora@example.com", client=_client(handler))
    pm.send(
        to="ceo@example.com",
        subject="Nora · draft reply",
        body="Here's a draft.",
        reply_to="abc123hash+corr99@inbound.postmarkapp.com",
    )

    assert captured["url"] == "https://api.postmarkapp.com/email"
    assert captured["token"] == "server-token"
    assert captured["body"]["From"] == "nora@example.com"
    assert captured["body"]["To"] == "ceo@example.com"
    assert captured["body"]["Subject"] == "Nora · draft reply"
    assert captured["body"]["TextBody"] == "Here's a draft."
    assert captured["body"]["ReplyTo"] == "abc123hash+corr99@inbound.postmarkapp.com"


def test_send_omits_reply_to_when_none() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"ErrorCode": 0})

    pm = PostmarkSendClient("t", "nora@example.com", client=_client(handler))
    pm.send(to="ceo@example.com", subject="s", body="b", reply_to=None)
    assert "ReplyTo" not in captured["body"]


def test_send_raises_on_non_2xx() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"ErrorCode": 300, "Message": "Invalid email"})

    pm = PostmarkSendClient("t", "nora@example.com", client=_client(handler))
    with pytest.raises(httpx.HTTPStatusError):
        pm.send(to="ceo@example.com", subject="s", body="b", reply_to=None)


def test_send_raises_on_transport_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    pm = PostmarkSendClient("t", "nora@example.com", client=_client(handler))
    with pytest.raises(httpx.ConnectError):
        pm.send(to="ceo@example.com", subject="s", body="b", reply_to=None)
