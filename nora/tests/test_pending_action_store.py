import time

from app.state.store import FilePendingActionStore, InMemoryPendingActionStore, PendingActionRecord


def _rec(token, **kw):
    base = dict(token=token, owner_secret_local="ceo1", recipient="x@third.com",
                subject="Meet", body="...", status="pending")
    base.update(kw)
    return PendingActionRecord(**base)


def test_record_has_reminder_fields_with_defaults():
    r = _rec("t1")
    assert r.reminders_sent == 0 and r.last_reminded_at is None


def test_all_pending_returns_only_live_pending():
    s = InMemoryPendingActionStore(ttl_seconds=10_000)
    s.put(_rec("t1"))
    s.put(_rec("t2", status="consumed"))
    s.put(_rec("t3", created_at=time.time() - 99_999))  # expired
    assert {r.token for r in s.all_pending()} == {"t1"}


def test_file_store_all_pending_round_trips(tmp_path):
    path = str(tmp_path / "pending.json")
    s = FilePendingActionStore(path, ttl_seconds=10_000)
    s.put(_rec("t1"))
    s.put(_rec("t2", status="consumed"))
    assert {r.token for r in s.all_pending()} == {"t1"}
