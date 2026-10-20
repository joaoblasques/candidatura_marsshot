from app.inbox_actions import parse_command


def test_star_and_archive():
    c = parse_command("star and archive")
    assert c.actions == ["star", "archive"] and c.scope == "rule" and c.control is None


def test_label_as_and_archive_captures_name():
    c = parse_command("label as ProjectX and archive")
    assert c.actions == ["label:ProjectX", "archive"]


def test_mark_read_and_important():
    assert parse_command("mark read").actions == ["read"]
    assert parse_command("mark important").actions == ["important"]
    assert parse_command("mark unread").actions == ["unread"]


def test_once_sets_scope():
    c = parse_command("star it, once")
    assert "star" in c.actions and c.scope == "once"


def test_stop_control_no_actions():
    c = parse_command("stop")
    assert c.control == "stop" and c.actions == []


def test_rules_query_control():
    assert parse_command("rules?").control == "list"
    assert parse_command("what rules do I have").control == "list"


def test_destructive_words_are_ignored():
    c = parse_command("delete it and trash")
    assert c.actions == []  # no safe action parsed; nothing destructive ever maps


def test_dedupes_and_preserves_order():
    assert parse_command("archive, star, archive").actions == ["archive", "star"]
