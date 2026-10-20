from app.state.store import (
    FileTriageStore,
    InMemoryTriageStore,
    TriageSessionRecord,
)


def _rec(token="t1", owner="nora-x", items=None):
    return TriageSessionRecord(token=token, owner_secret_local=owner,
                               items=items or ["thread one", "thread two"], subject="Fwd: batch")


def test_inmemory_put_get_delete():
    s = InMemoryTriageStore(ttl_seconds=1000)
    s.put(_rec())
    got = s.get("t1")
    assert got is not None and got.items == ["thread one", "thread two"]
    s.delete("t1")
    assert s.get("t1") is None


def test_inmemory_ttl_expiry():
    s = InMemoryTriageStore(ttl_seconds=0)  # everything is immediately stale
    s.put(_rec())
    assert s.get("t1") is None  # expired on read


def test_file_store_roundtrip_and_purge(tmp_path):
    path = str(tmp_path / "triage.json")
    s = FileTriageStore(path, ttl_seconds=1000)
    s.put(_rec(token="abc", items=["a", "b", "c"]))
    assert FileTriageStore(path, 1000).get("abc").items == ["a", "b", "c"]  # survives a new instance
    stale = FileTriageStore(path, ttl_seconds=0)
    assert stale.purge() == 1 and stale.get("abc") is None


def test_file_store_drops_unparseable_record(tmp_path):
    path = str(tmp_path / "triage.json")
    from app.state.json_file_map import atomic_write_json_map
    atomic_write_json_map(path, {"bad": {"not": "a valid record"}})
    assert FileTriageStore(path, 1000).get("bad") is None  # self-heals, no crash
