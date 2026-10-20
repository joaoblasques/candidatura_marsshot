from app.draft_feedback import FileDraftFeedbackStore
from scripts.draft_acceptance import format_readout


def test_readout_reports_counts_and_rate(tmp_path):
    s = FileDraftFeedbackStore(str(tmp_path / "f.json"))
    for i, sender in enumerate(["a@x.com", "b@x.com", "c@x.com", "d@x.com"]):
        s.track("me@x.com", f"d{i}", sender, f"t{i}")
    s.record_sent("me@x.com", "a@x.com")
    s.record_sent("me@x.com", "b@x.com")
    s.record_sent("me@x.com", "c@x.com")
    s.record_deletion("me@x.com", "d@x.com")  # 3 sent, 1 deleted, 0 pending
    out = format_readout(s.stats("me@x.com"), "me@x.com")
    assert "created:   4" in out
    assert "sent:      3" in out
    assert "deleted:   1" in out
    assert "pending:   0" in out
    assert "75%" in out


def test_readout_handles_no_decided_drafts(tmp_path):
    s = FileDraftFeedbackStore(str(tmp_path / "f.json"))
    s.track("me@x.com", "d0", "a@x.com", "t0")
    out = format_readout(s.stats("me@x.com"), "me@x.com")
    assert "pending:   1" in out and "n/a" in out
