"""Cross-thread relationship memory — Layer A store (metadata only)."""

from __future__ import annotations

from app.memory import FileMemoryStore, NullMemoryStore, RelationshipMemory, _MAX_PER_CEO


def test_null_store_is_off():
    s = NullMemoryStore()
    s.record("nora", "x@y.com", "2026-06-17")  # no-op, no error
    assert s.recall("nora", "x@y.com") is None


def test_file_store_records_and_recalls(tmp_path):
    s = FileMemoryStore(str(tmp_path / "mem.json"))
    assert s.recall("nora", "sarah@acme.com") is None
    s.record("nora", "sarah@acme.com", "2026-06-10")
    s.record("nora", "sarah@acme.com", "2026-06-17")
    mem = s.recall("nora", "sarah@acme.com")
    assert isinstance(mem, RelationshipMemory)
    assert mem.thread_count == 2 and mem.last_seen == "2026-06-17"


def test_file_store_isolates_per_ceo(tmp_path):
    s = FileMemoryStore(str(tmp_path / "mem.json"))
    s.record("nora", "sarah@acme.com", "2026-06-17")
    assert s.recall("democeo", "sarah@acme.com") is None  # different CEO, no leak


def test_file_store_bounds_per_ceo(tmp_path):
    s = FileMemoryStore(str(tmp_path / "mem.json"))
    for i in range(_MAX_PER_CEO + 10):
        s.record("nora", f"c{i:04d}@x.com", f"2026-01-{(i % 28) + 1:02d}")
    # the table never exceeds the per-CEO bound
    import json
    data = json.loads((tmp_path / "mem.json").read_text())
    assert len([k for k in data if k.startswith("nora|")]) <= _MAX_PER_CEO


def test_layerc_remember_recall_dedup_and_cap(tmp_path):
    from app.memory import _MAX_NOTES, FileMemoryStore
    s = FileMemoryStore(str(tmp_path / "mem.json"))
    s.remember("nora", "p@x.com", "prefers mornings")
    s.remember("nora", "p@x.com", "prefers mornings")  # dedup
    assert s.recall("nora", "p@x.com").notes == ["prefers mornings"]
    for i in range(_MAX_NOTES + 5):
        s.remember("nora", "p@x.com", f"fact {i}")
    assert len(s.recall("nora", "p@x.com").notes) == _MAX_NOTES  # newest-capped


def test_layerc_record_preserves_notes_and_forget_clears(tmp_path):
    from app.memory import FileMemoryStore
    s = FileMemoryStore(str(tmp_path / "mem.json"))
    s.remember("nora", "p@x.com", "assistant is Sarah")
    s.record("nora", "p@x.com", "2026-06-23")  # a later breadcrumb must not drop the fact
    assert s.recall("nora", "p@x.com").notes == ["assistant is Sarah"]
    s.forget_notes("nora", "p@x.com")
    assert s.recall("nora", "p@x.com").notes == []


def test_layerc_null_store_noops():
    from app.memory import NullMemoryStore
    n = NullMemoryStore()
    n.remember("nora", "p@x.com", "x")
    n.forget_notes("nora", "p@x.com")
    assert n.recall("nora", "p@x.com") is None


# --- Layer C v2 (auto-learn): provenance-tagged derived facts -------------------


def test_layerc_v2_learn_appends_dedup_and_caps(tmp_path):
    from app.memory import _MAX_DERIVED, FileMemoryStore
    s = FileMemoryStore(str(tmp_path / "mem.json"))
    s.learn("nora", "p@x.com", ["prefers mornings"])
    s.learn("nora", "p@x.com", ["Prefers Mornings"])  # case-insensitive dedup
    assert s.recall("nora", "p@x.com").derived == ["prefers mornings"]
    s.learn("nora", "p@x.com", [f"fact {i}" for i in range(_MAX_DERIVED + 5)])
    assert len(s.recall("nora", "p@x.com").derived) == _MAX_DERIVED  # newest-capped


def test_layerc_v2_learn_skips_facts_already_curated(tmp_path):
    from app.memory import FileMemoryStore
    s = FileMemoryStore(str(tmp_path / "mem.json"))
    s.remember("nora", "p@x.com", "is the Brightwave lead")
    s.learn("nora", "p@x.com", ["Is the Brightwave Lead", "prefers mornings"])
    mem = s.recall("nora", "p@x.com")
    assert mem.notes == ["is the Brightwave lead"]
    assert mem.derived == ["prefers mornings"]  # curated fact not re-learned


def test_layerc_v2_learn_caps_fact_length(tmp_path):
    from app.memory import FileMemoryStore
    s = FileMemoryStore(str(tmp_path / "mem.json"))
    s.learn("nora", "p@x.com", ["x" * 500])
    assert len(s.recall("nora", "p@x.com").derived[0]) == 160


def test_layerc_v2_record_preserves_derived(tmp_path):
    from app.memory import FileMemoryStore
    s = FileMemoryStore(str(tmp_path / "mem.json"))
    s.learn("nora", "p@x.com", ["prefers mornings"])
    s.record("nora", "p@x.com", "2026-06-29")  # breadcrumb must not drop derived facts
    assert s.recall("nora", "p@x.com").derived == ["prefers mornings"]


def test_layerc_v2_forget_clears_derived_and_notes(tmp_path):
    from app.memory import FileMemoryStore
    s = FileMemoryStore(str(tmp_path / "mem.json"))
    s.remember("nora", "p@x.com", "curated fact")
    s.learn("nora", "p@x.com", ["derived fact"])
    s.forget_notes("nora", "p@x.com")
    mem = s.recall("nora", "p@x.com")
    assert mem.notes == [] and mem.derived == []


def test_layerc_v2_null_learn_noop():
    from app.memory import NullMemoryStore
    n = NullMemoryStore()
    n.learn("nora", "p@x.com", ["x"])
    assert n.recall("nora", "p@x.com") is None
