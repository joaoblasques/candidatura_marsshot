from __future__ import annotations

from app.state.outbox import FileSendOutbox, InMemorySendOutbox, OutboxRecord


def _rec(token: str = "tok1") -> OutboxRecord:
    return OutboxRecord(
        token=token,
        recipient="sarah@acme.com",
        subject="Meeting request: Proposal",
        body="Hi Sarah ...",
        ceo_address="ceo@example.com",
        ceo_sender="ceo@nora-bennett.com",
        ceo_id="abc123hash",
        event_id="evt-1",
    )


def test_in_memory_outbox_round_trip() -> None:
    box = InMemorySendOutbox()
    assert box.pending() == []
    box.record(_rec("tok1"))
    box.record(_rec("tok2"))
    tokens = {r.token for r in box.pending()}
    assert tokens == {"tok1", "tok2"}
    box.remove("tok1")
    assert [r.token for r in box.pending()] == ["tok2"]
    box.remove("missing")  # no-op, never raises


def test_file_outbox_persists_across_instances(tmp_path) -> None:
    path = str(tmp_path / "outbox.json")
    FileSendOutbox(path).record(_rec("tok1"))
    # A fresh instance (simulating a restart) sees the persisted record.
    reread = FileSendOutbox(path)
    assert [r.token for r in reread.pending()] == ["tok1"]
    assert reread.pending()[0].recipient == "sarah@acme.com"
    reread.remove("tok1")
    assert FileSendOutbox(path).pending() == []


def test_file_outbox_missing_file_is_empty(tmp_path) -> None:
    assert FileSendOutbox(str(tmp_path / "nope.json")).pending() == []


def test_file_outbox_corrupt_file_is_empty_not_crash(tmp_path) -> None:
    path = tmp_path / "outbox.json"
    path.write_text("{ not json")
    assert FileSendOutbox(str(path)).pending() == []  # logged, not raised
