"""Outbound send: CEO-only by default, with the one consent-gated exception (U6)."""

from __future__ import annotations

import pytest

from app.email.outbound import OutboundSender
from app.receipt import ComposedReply


class FakeProviderClient:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    def send(
        self,
        *,
        to: str,
        subject: str,
        body: str,
        reply_to: str | None,
        sender: str | None = None,
        idempotency_key: str | None = None,
        attachments: list | None = None,
    ) -> None:
        self.sent.append(
            {
                "to": to,
                "subject": subject,
                "body": body,
                "reply_to": reply_to,
                "sender": sender,
                "idempotency_key": idempotency_key,
                "attachments": attachments,
            }
        )


def test_sends_to_ceo_only() -> None:
    client = FakeProviderClient()
    sender = OutboundSender(client)
    sender.send_to_ceo(
        "ceo@example.com",
        ComposedReply(subject="Nora · draft", body="..."),
        reply_to="nora-abc123+corr99@nora.ai",
    )
    assert len(client.sent) == 1
    assert client.sent[0]["to"] == "ceo@example.com"
    assert client.sent[0]["reply_to"] == "nora-abc123+corr99@nora.ai"


def test_only_two_public_send_methods() -> None:
    # R1/KTD7: send_to_ceo is the unconditional send; send_on_behalf is the SINGLE
    # consent-gated exception (0.2.0). This guards against a future edit quietly
    # adding a third send path that could originate to an arbitrary recipient.
    # Guard send *methods* specifically (a property like `supports_idempotency` is a
    # capability flag, not a send path).
    public = [
        m for m in dir(OutboundSender)
        if not m.startswith("_") and callable(getattr(OutboundSender, m))
    ]
    assert public == ["send_on_behalf", "send_to_ceo"]


def test_send_on_behalf_targets_recipient_from_assistant_address() -> None:
    client = FakeProviderClient()
    sender = OutboundSender(client)
    sender.send_on_behalf(
        "sarah@acme.com",
        ComposedReply(subject="Meeting request: Proposal", body="Hi Sarah ..."),
        sender="nora@nora-bennett.com",
        reply_to="ceo@example.com",
        idempotency_key="corr-token-123",
    )
    assert len(client.sent) == 1
    sent = client.sent[0]
    assert sent["to"] == "sarah@acme.com"
    assert sent["sender"] == "nora@nora-bennett.com"  # Nora's own labeled From, never the CEO
    assert sent["reply_to"] == "ceo@example.com"  # the third party's reply routes to the CEO
    assert sent["idempotency_key"] == "corr-token-123"


def test_send_on_behalf_refuses_empty_sender() -> None:
    # Fail closed: an empty From would risk an unlabeled/wrong send. Better to raise
    # (→ failure receipt) than to send from nothing.
    client = FakeProviderClient()
    sender = OutboundSender(client)
    with pytest.raises(ValueError):
        sender.send_on_behalf(
            "sarah@acme.com",
            ComposedReply(subject="s", body="b"),
            sender="",
            reply_to="ceo@example.com",
        )
    assert client.sent == []  # nothing went out


def test_signature_appended_once_and_idempotent():
    from app.email.outbound import SIGNATURE, _with_signature
    body = "Your draft is ready."
    signed = _with_signature(body)
    assert signed.endswith(SIGNATURE) and "https://docs.nora-bennett.com/" in signed
    # already-signed body is not double-signed (e.g. an outbox replay)
    assert _with_signature(signed) == signed
