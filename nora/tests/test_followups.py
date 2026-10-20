from app.followups import due_for_reminder, reminder_reply, run_followups
from app.state.store import PendingActionRecord

NOW = 1_000_000.0
DAY = 86_400.0


def _rec(token, *, age_days, **kw):
    base = dict(token=token, owner_secret_local="ceo1", recipient="x@third.com",
                subject="Meet", body="...", status="pending",
                created_at=NOW - age_days * DAY)
    base.update(kw)
    return PendingActionRecord(**base)


def test_due_after_threshold_only():
    recs = [_rec("young", age_days=1), _rec("old", age_days=3)]
    due = due_for_reminder(recs, NOW, remind_after_seconds=2 * DAY, max_reminders=1)
    assert [r.token for r in due] == ["old"]


def test_consumed_and_capped_are_skipped():
    recs = [
        _rec("done", age_days=3, status="consumed"),
        _rec("maxed", age_days=3, reminders_sent=1),
        _rec("ok", age_days=3),
    ]
    due = due_for_reminder(recs, NOW, remind_after_seconds=2 * DAY, max_reminders=1)
    assert [r.token for r in due] == ["ok"]


class _FakeStore:
    def __init__(self, recs):
        self._recs = {r.token: r for r in recs}

    def all_pending(self):
        return [r for r in self._recs.values() if r.status == "pending"]

    def put(self, r):
        self._recs[r.token] = r


def test_run_reminds_due_to_ceo_and_marks_idempotent():
    store = _FakeStore([_rec("old", age_days=3)])
    sent = []
    n = run_followups(
        store, lambda ceo, rec: sent.append((ceo, rec.token)),
        resolve_ceo=lambda local: "ceo@me.com", now=NOW,
        remind_after_seconds=2 * DAY, max_reminders=1,
    )
    assert n == 1 and sent == [("ceo@me.com", "old")]
    r = store._recs["old"]
    assert r.reminders_sent == 1 and r.last_reminded_at == NOW
    sent.clear()
    assert run_followups(store, lambda c, x: sent.append(c), resolve_ceo=lambda _local: "ceo@me.com",
                         now=NOW, remind_after_seconds=2 * DAY, max_reminders=1) == 0
    assert sent == []


def test_run_skips_when_ceo_unresolved_and_never_targets_third_party():
    rec = _rec("old", age_days=3)
    store = _FakeStore([rec])
    targets = []
    run_followups(store, lambda ceo, r: targets.append(ceo), resolve_ceo=lambda local: None,
                  now=NOW, remind_after_seconds=2 * DAY, max_reminders=1)
    assert targets == []  # unresolved CEO -> skipped; nothing sent
    assert rec.recipient == "x@third.com"  # the third party was never a send target


def test_reminder_reply_summarizes_and_says_AG():
    rec = _rec("t", age_days=3)
    reply = reminder_reply(rec, lapse_at=NOW + 2 * DAY)
    assert "AG" in reply.body and "Meet" in reply.subject
    assert "x@third.com" in reply.body
    assert "lapses" in reply.body.lower()
