from app.voice_memory import (
    VOICE_CODES,
    FileVoiceStore,
    NullVoiceStore,
    build_voice_profile,
    consolidate_store,
)

DAY = 86_400.0


def test_null_voice_is_off():
    n = NullVoiceStore()
    n.record("ceo1", ["SHORT"], 1.0)
    assert n.profile("ceo1") == "" and n.signals("ceo1") == []


def test_file_store_records_only_voice_codes(tmp_path):
    s = FileVoiceStore(str(tmp_path / "v.json"))
    s.record("ceo1", ["SHORT", "F", "SV", "RE"], when=10.0)  # SV/RE are not voice codes
    codes = [sig["code"] for sig in s.signals("ceo1")]
    assert sorted(codes) == ["F", "SHORT"]
    assert "SHORT" in VOICE_CODES


def test_forget_wipes_a_ceo_only(tmp_path):
    s = FileVoiceStore(str(tmp_path / "v.json"))
    s.record("ceo1", ["SHORT"], when=1.0)
    s.record("ceo2", ["F"], when=1.0)
    s.forget("ceo1")
    assert s.signals("ceo1") == [] and [x["code"] for x in s.signals("ceo2")] == ["F"]


def test_set_profile_round_trips(tmp_path):
    s = FileVoiceStore(str(tmp_path / "v.json"))
    s.set_profile("ceo1", "Preferred style: concise.")
    assert s.profile("ceo1") == "Preferred style: concise."


# --- Layer B v2: per-recipient voice profiles -----------------------------------


def test_key_composes_global_vs_scoped():
    from app.voice_memory import _key
    assert _key("ceo1") == "ceo1"
    assert _key("ceo1", "sarah@acme.com") == "ceo1|sarah@acme.com"


def test_per_recipient_signals_isolated_from_global_and_each_other(tmp_path):
    s = FileVoiceStore(str(tmp_path / "v.json"))
    s.record("ceo1", ["F"], when=1.0, counterparty="board@x.com")
    s.record("ceo1", ["SHORT"], when=1.0, counterparty="pal@y.com")
    s.record("ceo1", ["LONG"], when=1.0)  # global
    assert [x["code"] for x in s.signals("ceo1", "board@x.com")] == ["F"]
    assert [x["code"] for x in s.signals("ceo1", "pal@y.com")] == ["SHORT"]
    assert [x["code"] for x in s.signals("ceo1")] == ["LONG"]


def test_per_recipient_profile_round_trips(tmp_path):
    s = FileVoiceStore(str(tmp_path / "v.json"))
    s.set_profile("ceo1", "Preferred style: formal.", counterparty="board@x.com")
    assert s.profile("ceo1", "board@x.com") == "Preferred style: formal."
    assert s.profile("ceo1") == ""  # global untouched


def test_forget_wipes_global_and_all_per_recipient_keys(tmp_path):
    s = FileVoiceStore(str(tmp_path / "v.json"))
    s.record("ceo1", ["LONG"], when=1.0)
    s.record("ceo1", ["F"], when=1.0, counterparty="board@x.com")
    s.record("ceo1", ["SHORT"], when=1.0, counterparty="pal@y.com")
    s.record("ceo2", ["F"], when=1.0, counterparty="board@x.com")  # other CEO untouched
    s.forget("ceo1")
    assert s.signals("ceo1") == []
    assert s.signals("ceo1", "board@x.com") == [] and s.signals("ceo1", "pal@y.com") == []
    assert [x["code"] for x in s.signals("ceo2", "board@x.com")] == ["F"]


def test_null_store_accepts_counterparty():
    n = NullVoiceStore()
    n.record("ceo1", ["SHORT"], 1.0, counterparty="x@y.com")
    assert n.profile("ceo1", "x@y.com") == "" and n.signals("ceo1", "x@y.com") == []


def test_dream_consolidates_per_recipient_keys(tmp_path):
    # The dream's all-keys pass must build a per-recipient profile via the composite key.
    from app.voice_dream import consolidate_all
    from app.state.json_file_map import read_json_map
    s = FileVoiceStore(str(tmp_path / "v.json"))
    now = 100 * DAY
    s.record("ceo1", ["F"], when=now, counterparty="board@x.com")
    keys = list(read_json_map(str(tmp_path / "v.json")).keys())
    consolidate_all(s, keys, now, ttl_seconds=90 * DAY)
    assert "formal" in s.profile("ceo1", "board@x.com")


def test_build_profile_aggregates_and_resolves_opposites():
    now = 100 * DAY
    sigs = (
        [{"code": "SHORT", "when": now}] * 3
        + [{"code": "LONG", "when": now}]  # Short wins the length pair
        + [{"code": "F", "when": now}] * 2
        + [{"code": "MN", "when": now}]
    )
    p = build_voice_profile(sigs, now, ttl_seconds=90 * DAY)
    assert "concise" in p and "formal" in p and "your name" in p
    assert "detailed" not in p


def test_build_profile_decays_old_signals():
    now = 100 * DAY
    old = [{"code": "SHORT", "when": now - 200 * DAY}]
    assert build_voice_profile(old, now, ttl_seconds=90 * DAY) == ""


def test_consolidate_store_prunes_and_sets_profile(tmp_path):
    s = FileVoiceStore(str(tmp_path / "v.json"))
    now = 100 * DAY
    s.record("ceo1", ["SHORT"], when=now)
    s.record("ceo1", ["SHORT"], when=now - 200 * DAY)  # stale
    prof = consolidate_store(s, "ceo1", now, ttl_seconds=90 * DAY)
    assert "concise" in prof and s.profile("ceo1") == prof
    assert all((now - x["when"]) <= 90 * DAY for x in s.signals("ceo1"))


def test_consolidate_all_builds_profiles(tmp_path):
    from app.voice_dream import consolidate_all
    s = FileVoiceStore(str(tmp_path / "v.json"))
    now = 100 * DAY
    s.record("ceo1", ["SHORT"], when=now)
    s.record("ceo2", ["F"], when=now)
    n = consolidate_all(s, ["ceo1", "ceo2"], now, ttl_seconds=90 * DAY)
    assert n == 2 and "concise" in s.profile("ceo1") and "formal" in s.profile("ceo2")
