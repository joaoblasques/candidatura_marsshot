from app.draft_feedback import FileDraftFeedbackStore, NullDraftFeedbackStore


def test_track_pending_resolve(tmp_path):
    s = FileDraftFeedbackStore(str(tmp_path / "f.json"))
    assert s.pending("me") == {}
    s.track("me", "draft1", "alice@acme.com", "thread1")
    assert s.pending("me") == {"draft1": ("alice@acme.com", "thread1")}
    s.resolve("me", "draft1")
    assert s.pending("me") == {}


def test_deletion_increments_and_suppresses_at_two(tmp_path):
    s = FileDraftFeedbackStore(str(tmp_path / "f.json"))
    assert s.suppressed("me", "alice@acme.com") is False
    assert s.record_deletion("me", "alice@acme.com") == 1
    assert s.suppressed("me", "alice@acme.com") is False   # one is a maybe
    assert s.record_deletion("me", "alice@acme.com") == 2
    assert s.suppressed("me", "alice@acme.com") is True     # two is a pattern


def test_sent_resets_deletions_and_suppression(tmp_path):
    s = FileDraftFeedbackStore(str(tmp_path / "f.json"))
    s.record_deletion("me", "alice@acme.com")
    s.record_deletion("me", "alice@acme.com")
    assert s.suppressed("me", "alice@acme.com") is True
    s.record_sent("me", "alice@acme.com")
    assert s.suppressed("me", "alice@acme.com") is False
    assert s.record_deletion("me", "alice@acme.com") == 1   # counter reset


def test_owner_and_sender_isolation(tmp_path):
    s = FileDraftFeedbackStore(str(tmp_path / "f.json"))
    s.record_deletion("me", "a@x.com")
    s.record_deletion("me", "a@x.com")
    assert s.suppressed("me", "a@x.com") is True
    assert s.suppressed("other", "a@x.com") is False
    assert s.suppressed("me", "b@x.com") is False


def test_null_store_noops():
    n = NullDraftFeedbackStore()
    n.track("me", "d1", "a@x.com", "t1")
    assert n.record_deletion("me", "a@x.com") == 0
    assert n.pending("me") == {} and n.suppressed("me", "a@x.com") is False
    assert n.stats("me") == {"created": 0, "sent": 0, "deleted": 0, "acceptance_rate": None}


def test_stats_track_created_sent_deleted_and_acceptance_rate(tmp_path):
    s = FileDraftFeedbackStore(str(tmp_path / "f.json"))
    assert s.stats("me") == {"created": 0, "sent": 0, "deleted": 0, "acceptance_rate": None}
    s.track("me", "d1", "a@x.com", "t1")
    s.track("me", "d2", "b@x.com", "t2")
    s.track("me", "d3", "c@x.com", "t3")
    s.record_sent("me", "a@x.com")      # d1 sent
    s.record_sent("me", "b@x.com")      # d2 sent
    s.record_deletion("me", "c@x.com")  # d3 deleted
    st = s.stats("me")
    assert st["created"] == 3 and st["sent"] == 2 and st["deleted"] == 1
    assert st["acceptance_rate"] == 2 / 3   # sent / (sent + deleted) among decided drafts


def test_stats_are_owner_scoped(tmp_path):
    s = FileDraftFeedbackStore(str(tmp_path / "f.json"))
    s.track("me", "d1", "a@x.com", "t1")
    assert s.stats("other")["created"] == 0
