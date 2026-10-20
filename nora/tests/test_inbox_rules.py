from app.inbox_rules import FileInboxRuleStore, NullInboxRuleStore


def test_set_get_clear_round_trip(tmp_path):
    s = FileInboxRuleStore(str(tmp_path / "rules.json"))
    assert s.get("me", "alice@acme.com") is None
    s.set("me", "alice@acme.com", ["star", "archive"])
    assert s.get("me", "alice@acme.com") == ["star", "archive"]
    s.clear("me", "alice@acme.com")
    assert s.get("me", "alice@acme.com") is None


def test_owner_and_sender_isolation(tmp_path):
    s = FileInboxRuleStore(str(tmp_path / "rules.json"))
    s.set("me", "alice@acme.com", ["archive"])
    s.set("me", "bob@x.com", ["star"])
    s.set("other", "alice@acme.com", ["important"])
    assert s.get("me", "bob@x.com") == ["star"]
    assert s.get("other", "alice@acme.com") == ["important"]
    assert s.all("me") == {"alice@acme.com": ["archive"], "bob@x.com": ["star"]}


def test_case_insensitive_key(tmp_path):
    s = FileInboxRuleStore(str(tmp_path / "rules.json"))
    s.set("Me", "Alice@Acme.com", ["archive"])
    assert s.get("me", "alice@acme.com") == ["archive"]


def test_null_store_noops():
    n = NullInboxRuleStore()
    n.set("me", "a@x.com", ["star"])
    assert n.get("me", "a@x.com") is None and n.all("me") == {}
