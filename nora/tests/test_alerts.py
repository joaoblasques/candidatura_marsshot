"""Real-time ops failure-alert (app/alerts.py). Hermetic; asserts no PII."""

from __future__ import annotations

from app.alerts import AlertThrottle, maybe_send_failure_alert
from app.config import Settings
from app.metrics import hash_id


class _Outbox:
    """Minimal stand-in for OutboundSender that captures (or fails) sends."""

    def __init__(self, fail: bool = False):
        self.sent: list[dict] = []
        self.fail = fail

    def send_to_ceo(self, ceo_address, reply, reply_to=None, sender=None):
        if self.fail:
            raise RuntimeError("send boom")
        self.sent.append(
            {"to": ceo_address, "subject": reply.subject, "body": reply.body, "sender": sender}
        )


def _settings(**kw) -> Settings:
    base = dict(
        external_llm_ack=True,
        anthropic_api_key="k",
        webhook_basic_auth="u:p",
        postmark_sender="nora@example.com",
        metrics_salt="s",
        alert_email="ops@example.com",
    )
    base.update(kw)
    return Settings(**base)


# --- AlertThrottle ----------------------------------------------------------

def test_throttle_allows_then_suppresses_then_allows() -> None:
    now = [0.0]
    thr = AlertThrottle(100, clock=lambda: now[0])
    assert thr.allow() is True       # first
    now[0] = 50
    assert thr.allow() is False      # within window
    now[0] = 100
    assert thr.allow() is True       # window elapsed


# --- maybe_send_failure_alert ----------------------------------------------

def test_alert_sent_to_maintainer_with_no_pii() -> None:
    out = _Outbox()
    sent = maybe_send_failure_alert(
        out, AlertThrottle(0), _settings(), "nora", "evt-1", "ceo@example.com", "nora@nora-bennett.com"
    )
    assert sent is True and len(out.sent) == 1
    rec = out.sent[0]
    assert rec["to"] == "ops@example.com"
    blob = rec["subject"] + rec["body"]
    # No email content / sender address leaks; identity is the salted hash only.
    for leaked in ("Secret subject line", "confidential body text", "ceo@example.com"):
        assert leaked not in blob
    assert hash_id("nora", "s") in rec["body"]
    assert "evt-1" in rec["body"]


def test_no_alert_when_email_unset() -> None:
    out = _Outbox()
    assert maybe_send_failure_alert(
        out, AlertThrottle(0), _settings(alert_email=""), "nora", "evt-1", "ceo@example.com", "s"
    ) is False
    assert out.sent == []


def test_no_alert_when_recipient_is_the_affected_ceo() -> None:
    # Operator == the CEO who already got the failure receipt → don't double-email.
    out = _Outbox()
    assert maybe_send_failure_alert(
        out, AlertThrottle(0), _settings(alert_email="ceo@example.com"), "nora", "evt-1",
        "ceo@example.com", "s",
    ) is False
    assert out.sent == []


def test_throttle_suppresses_second_alert() -> None:
    out = _Outbox()
    thr = AlertThrottle(1000)  # large window
    first = maybe_send_failure_alert(out, thr, _settings(), "nora", "evt-1", "ceo@example.com", "s")
    second = maybe_send_failure_alert(out, thr, _settings(), "nora", "evt-1", "ceo@example.com", "s")
    assert first is True and second is False
    assert len(out.sent) == 1


def test_alert_send_failure_never_raises() -> None:
    out = _Outbox(fail=True)
    assert maybe_send_failure_alert(
        out, AlertThrottle(0), _settings(), "nora", "evt-1", "ceo@example.com", "s"
    ) is False  # no exception propagates
