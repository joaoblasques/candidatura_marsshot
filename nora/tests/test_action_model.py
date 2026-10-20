from __future__ import annotations

from app.pipeline import ActionDescriptor
from app.state.store import PendingActionRecord


def test_pending_action_defaults_send_email() -> None:
    r = PendingActionRecord(token="t", owner_secret_local="o", recipient="x@y.com",
                            subject="s", body="b")
    assert r.action_kind == "send_email"
    assert r.payload == {}


def test_pending_action_calendar_kind_with_payload() -> None:
    r = PendingActionRecord(token="t", owner_secret_local="o", recipient="x@y.com",
                            subject="s", body="b", action_kind="calendar_hold",
                            payload={"summary": "Sync", "start": "s", "end": "e"})
    assert r.action_kind == "calendar_hold" and r.payload["summary"] == "Sync"


def test_action_descriptor_carries_kind_and_payload() -> None:
    d = ActionDescriptor(recipient="x@y.com", subject="s", body="b", token="t",
                         action_kind="calendar_hold", payload={"summary": "Sync"})
    assert d.action_kind == "calendar_hold" and d.payload["summary"] == "Sync"
