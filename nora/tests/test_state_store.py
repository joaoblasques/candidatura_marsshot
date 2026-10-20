"""U2: pending consent-gated action store (separate from the revise store)."""

from __future__ import annotations

from app.state.store import (
    FilePendingActionStore,
    InMemoryPendingActionStore,
    PendingActionRecord,
    new_correlation_token,
)


def _record(token: str | None = None, status: str = "pending") -> PendingActionRecord:
    return PendingActionRecord(
        token=token or new_correlation_token(),
        owner_secret_local="nora-abc123",
        recipient="sarah@acme.com",
        subject="Meeting next week?",
        body="Hi Sarah — I'm Nora, EA to the CEO. Could we find time next week?",
        status=status,
    )


def test_put_get_delete_round_trip() -> None:
    store = InMemoryPendingActionStore(3600)
    rec = _record()
    store.put(rec)
    got = store.get(rec.token)
    assert got is not None and got.recipient == "sarah@acme.com" and got.status == "pending"
    store.delete(rec.token)
    assert store.get(rec.token) is None


def test_expired_record_not_returned() -> None:
    store = InMemoryPendingActionStore(0)  # everything is immediately stale
    rec = _record()
    store.put(rec)
    assert store.get(rec.token) is None


def test_purge_removes_stale() -> None:
    store = InMemoryPendingActionStore(0)
    store.put(_record())
    store.put(_record())
    assert store.purge() == 2


def test_consumed_status_persists() -> None:
    # The single-use guard is the status field; the store round-trips it faithfully.
    store = InMemoryPendingActionStore(3600)
    rec = _record(status="consumed")
    store.put(rec)
    assert store.get(rec.token).status == "consumed"


def test_unknown_token_returns_none() -> None:
    assert InMemoryPendingActionStore(3600).get("nope") is None


def _pa(token: str = "t1", created_at: float | None = None) -> PendingActionRecord:
    kw = dict(token=token, owner_secret_local="abc", recipient="sarah@acme.com",
              subject="Meeting request: x", body="Hi Sarah ...")
    if created_at is not None:
        kw["created_at"] = created_at
    return PendingActionRecord(**kw)


def test_file_pending_action_round_trip_and_persistence(tmp_path) -> None:
    path = str(tmp_path / "pa.json")
    FilePendingActionStore(path, 3600).put(_pa("t1"))
    store = FilePendingActionStore(path, 3600)  # fresh instance = restart
    got = store.get("t1")
    assert got is not None and got.recipient == "sarah@acme.com"
    store.delete("t1")
    assert FilePendingActionStore(path, 3600).get("t1") is None


def test_file_pending_action_get_expired_returns_none(tmp_path) -> None:
    import time
    path = str(tmp_path / "pa.json")
    FilePendingActionStore(path, 1).put(_pa("t1", created_at=time.time() - 10))
    assert FilePendingActionStore(path, 1).get("t1") is None


def test_file_pending_action_purge_drops_expired(tmp_path) -> None:
    import time
    path = str(tmp_path / "pa.json")
    store = FilePendingActionStore(path, 1)
    store.put(_pa("fresh"))
    store.put(_pa("old", created_at=time.time() - 10))
    assert store.purge() == 1
    assert store.get("fresh") is not None and store.get("old") is None


def test_file_pending_action_put_failure_is_best_effort(tmp_path, monkeypatch) -> None:
    path = str(tmp_path / "pa.json")
    store = FilePendingActionStore(path, 3600)
    import app.state.store as store_mod

    def _boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(store_mod, "atomic_write_json_map", _boom)
    store.put(_pa("t1"))  # must NOT raise


def test_file_pending_action_tolerates_malformed_record(tmp_path) -> None:
    import json

    path = tmp_path / "pa.json"
    # A valid-JSON map whose "bad" record no longer fits the model (schema drift).
    path.write_text(json.dumps({
        "good": _pa("good").model_dump(),
        "bad": {"token": "bad", "unexpected": True},
    }))
    store = FilePendingActionStore(str(path), 3600)
    assert store.get("good") is not None   # good record unaffected by the malformed sibling
    assert store.purge() >= 1              # malformed record dropped — file self-heals
    assert store.get("bad") is None
    assert store.get("good") is not None
