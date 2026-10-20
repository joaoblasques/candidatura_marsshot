"""SMTP outbound send client (fake SMTP connection, no network)."""

from __future__ import annotations

import smtplib

import pytest

from app.email.smtp_send import SmtpSendClient


class _FakeSMTP:
    def __init__(self) -> None:
        self.sent: list = []
        self.tls = False
        self.login_args: tuple | None = None

    def __enter__(self) -> _FakeSMTP:
        return self

    def __exit__(self, *exc) -> bool:
        return False

    def starttls(self) -> None:
        self.tls = True

    def login(self, user: str, password: str) -> None:
        self.login_args = (user, password)

    def send_message(self, message) -> None:
        self.sent.append(message)


def test_smtp_send_builds_and_sends_message() -> None:
    fake = _FakeSMTP()
    client = SmtpSendClient("h", 1025, "nora@local", smtp_factory=lambda: fake)
    client.send(
        to="ceo@example.com",
        subject="Nora · draft reply",
        body="Here's a draft.",
        reply_to="abc123hash+corr99@inbound.postmarkapp.com",
    )
    assert len(fake.sent) == 1
    msg = fake.sent[0]
    assert msg["From"] == "nora@local"
    assert msg["To"] == "ceo@example.com"
    assert msg["Subject"] == "Nora · draft reply"
    assert msg["Reply-To"] == "abc123hash+corr99@inbound.postmarkapp.com"
    assert msg.get_content().strip() == "Here's a draft."
    assert fake.tls is False
    assert fake.login_args is None


def test_smtp_send_omits_reply_to_when_none() -> None:
    fake = _FakeSMTP()
    client = SmtpSendClient("h", 1025, "nora@local", smtp_factory=lambda: fake)
    client.send(to="ceo@example.com", subject="s", body="b", reply_to=None)
    assert fake.sent[0]["Reply-To"] is None  # header absent


def test_smtp_send_uses_tls_and_login_when_configured() -> None:
    fake = _FakeSMTP()
    client = SmtpSendClient(
        "h", 587, "nora@local", username="u", password="p", use_tls=True,
        smtp_factory=lambda: fake,
    )
    client.send(to="x@y.com", subject="s", body="b", reply_to=None)
    assert fake.tls is True
    assert fake.login_args == ("u", "p")


def test_smtp_send_propagates_errors() -> None:
    class _Boom(_FakeSMTP):
        def send_message(self, message) -> None:
            raise smtplib.SMTPException("server down")

    client = SmtpSendClient("h", 1025, "nora@local", smtp_factory=lambda: _Boom())
    with pytest.raises(smtplib.SMTPException):
        client.send(to="x@y.com", subject="s", body="b", reply_to=None)
