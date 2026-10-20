from __future__ import annotations

from app.drafting.json_backend import parse_json_content, strip_trailing_name_placeholder


def test_parse_plain_json():
    assert parse_json_content('{"a": 1}') == {"a": 1}


def test_parse_strips_markdown_fence():
    assert parse_json_content('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json_content('```\n{"a": 2}\n```') == {"a": 2}


def test_parse_recovers_json_amid_prose():
    assert parse_json_content('Sure! Here it is: {"a": 1} — hope that helps') == {"a": 1}


def test_parse_returns_none_on_garbage_or_nondict():
    assert parse_json_content("no json here") is None
    assert parse_json_content("[1, 2, 3]") is None  # a list is not the field object
    assert parse_json_content("") is None
    assert parse_json_content(None) is None


def test_strip_placeholder_only_lone_final_line():
    assert strip_trailing_name_placeholder("Hi,\n\nBest,\n\nCEO") == "Hi,\n\nBest,"
    assert strip_trailing_name_placeholder("Hi,\n\nThe CEO will join.") == "Hi,\n\nThe CEO will join."
