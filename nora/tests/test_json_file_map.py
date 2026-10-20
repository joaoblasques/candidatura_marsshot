from __future__ import annotations

from app.state.json_file_map import atomic_write_json_map, read_json_map


def test_round_trip(tmp_path) -> None:
    path = str(tmp_path / "m.json")
    atomic_write_json_map(path, {"a": {"x": 1}})
    assert read_json_map(path) == {"a": {"x": 1}}


def test_missing_file_is_empty(tmp_path) -> None:
    assert read_json_map(str(tmp_path / "nope.json")) == {}


def test_corrupt_file_is_empty_not_raise(tmp_path) -> None:
    p = tmp_path / "m.json"
    p.write_text("{ not json")
    assert read_json_map(str(p)) == {}


def test_non_dict_json_is_empty(tmp_path) -> None:
    p = tmp_path / "m.json"
    p.write_text("[1, 2, 3]")
    assert read_json_map(str(p)) == {}
