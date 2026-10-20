from app.triage import relationship_hint, split_threads

GMAIL = "---------- Forwarded message ----------"
APPLE = "Begin forwarded message:"


def test_relationship_hint_first_time():
    assert relationship_hint(0, "", []) == "first-time sender"


def test_relationship_hint_recurring_with_count_and_date():
    h = relationship_hint(4, "2026-06-20", [])
    assert "recurring sender" in h and "4 prior exchanges" in h and "2026-06-20" in h


def test_relationship_hint_singular_exchange():
    assert "1 prior exchange" in relationship_hint(1, "2026-06-20", [])
    assert "1 prior exchanges" not in relationship_hint(1, "2026-06-20", [])


def test_relationship_hint_appends_curated_notes():
    h = relationship_hint(3, "2026-06-20", ["prefers mornings"])
    assert "recurring sender" in h and "you noted: prefers mornings" in h


def test_splits_bundled_forward_on_markers_dropping_lead_note():
    body = f"Triage these.\n{GMAIL}\nFrom: a@x.com\nHi A\n{GMAIL}\nFrom: b@y.com\nHi B"
    items = split_threads(body)
    assert len(items) == 2
    assert "Hi A" in items[0] and "a@x.com" in items[0]
    assert "Hi B" in items[1]
    assert "Triage these." not in "".join(items)  # the CEO's lead note is dropped


def test_apple_mail_marker_also_splits():
    body = f"{APPLE}\nthread one\n{APPLE}\nthread two"
    assert len(split_threads(body)) == 2


def test_no_marker_is_single_item():
    assert split_threads("just one thread, no markers") == ["just one thread, no markers"]


def test_empty_body_is_no_items():
    assert split_threads("   ") == []


def test_caps_at_max_items():
    body = "".join(f"{GMAIL}\nthread {i}\n" for i in range(30))
    assert len(split_threads(body, max_items=20)) == 20


def test_parse_triage_followup_basic_and_order():
    from app.triage import parse_triage_followup
    items = parse_triage_followup("2 RE [keep it warm]\n4 S\nRE 1", n_items=5)
    assert [(i.index, i.action_code, i.notes) for i in items] == [
        (2, "RE", "keep it warm"), (4, "S", None), (1, "RE", None)
    ]
    assert all(i.in_range and i.supported for i in items)


def test_parse_triage_followup_ignores_junk_and_dedups():
    from app.triage import parse_triage_followup
    items = parse_triage_followup("hello there\n2 RE\n2 S\njust text\n", n_items=5)
    assert [(i.index, i.action_code) for i in items] == [(2, "RE")]  # junk dropped; first wins


def test_parse_triage_followup_flags_out_of_range_and_unsupported():
    from app.triage import parse_triage_followup
    items = parse_triage_followup("9 RE\n1 SM", n_items=3)
    by_index = {i.index: i for i in items}
    assert by_index[9].in_range is False
    assert by_index[1].in_range is True and by_index[1].supported is False  # SM not per-item
