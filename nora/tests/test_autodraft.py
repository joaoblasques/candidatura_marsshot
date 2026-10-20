from app.autodraft import autodraft_once, is_command, is_personal_inbound


def test_is_command_detects_plus_alias():
    assert is_command({"to": "Me <me+nora@x.com>"}, "nora") is True
    assert is_command({"delivered-to": "me+nora@x.com"}, "nora") is True
    assert is_command({"to": "me@x.com"}, "nora") is False


class _FakeProvider:
    def __init__(self, found="found1"):
        self.found = found
        self.applied = []

    def find_message(self, sender, subject, msgid):
        self.last = (sender, subject, msgid)
        return self.found

    def apply_actions(self, mid, actions):
        self.applied.append((mid, actions))


def test_command_applies_actions_and_does_not_draft():
    cmd = _msg(
        "c1",
        {"From": "me@x.com", "To": "me+nora@x.com", "Subject": "Fwd: Hello",
         "References": "<orig-1@x>"},
        body_text=("star and archive\n"
                   "---------- Forwarded message ----------\n"
                   "From: alice@acme.com\nSubject: Hello\nHi there"),
    )
    svc = _FakeService([cmd])
    prov = _FakeProvider()
    drafted_calls = []
    n = autodraft_once(
        svc, user_id="me", label_id="Label_9",
        draft_reply=lambda *a: drafted_calls.append(a) or None,
        save_draft=lambda *a, **k: "d",
        provider=prov, alias="nora", inbox_actions=True,
    )
    assert prov.applied == [("found1", ["star", "archive"])]  # actions applied to the ORIGINAL
    assert prov.last[0] == "alice@acme.com" and prov.last[2] == "<orig-1@x>"
    assert drafted_calls == [] and n == 0  # a command is never drafted
    # the command forward is labelled AND archived (−INBOX) so it doesn't linger in the inbox
    assert svc.messages_api.modified == [
        ("c1", {"addLabelIds": ["Label_9"], "removeLabelIds": ["INBOX"]})
    ]


class _Exec:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


def _msg(mid, headers, *, thread_id="t-" + "x", body_text="hello"):
    import base64
    raw = base64.urlsafe_b64encode(body_text.encode()).decode()
    return {
        "id": mid, "threadId": thread_id,
        "payload": {
            "headers": [{"name": k, "value": v} for k, v in headers.items()],
            "mimeType": "text/plain", "body": {"data": raw},
        },
    }


class _FakeMessages:
    def __init__(self, msgs):
        self._msgs = {m["id"]: m for m in msgs}
        self.modified = []

    def list(self, *, userId, q, maxResults):  # noqa: N803
        return _Exec({"messages": [{"id": i} for i in self._msgs]})

    def get(self, *, userId, id, format):  # noqa: A002,N803
        return _Exec(self._msgs[id])

    def modify(self, *, userId, id, body):  # noqa: N803
        self.modified.append((id, body))
        return _Exec({})


class _FakeService:
    def __init__(self, msgs):
        self.messages_api = _FakeMessages(msgs)

    def users(self):
        return self

    def messages(self):
        return self.messages_api


def test_autodraft_once_drafts_personal_skips_newsletter_labels_both():
    from app.models import NoraResult, ResultKind
    personal = _msg("m1", {"From": "Alice <alice@acme.com>", "Subject": "Lunch?",
                           "Message-ID": "<a-1@acme.com>"}, thread_id="th-1")
    news = _msg("m2", {"From": "News <news@list.com>", "Subject": "Weekly",
                       "List-Unsubscribe": "<mailto:u@list.com>"}, thread_id="th-2")
    svc = _FakeService([personal, news])
    saved = []

    def draft_reply(sender, subject, body):
        return NoraResult(kind=ResultKind.DRAFT, text="Hi Alice,", interpretation_readback="")

    def save_draft(to, subject, body, *, thread_id=None, in_reply_to=None):
        saved.append((to, subject, thread_id, in_reply_to))
        return "draft-x"

    n = autodraft_once(svc, user_id="me", label_id="Label_9",
                       draft_reply=draft_reply, save_draft=save_draft)
    assert n == 1  # only the personal email drafted
    assert saved == [("alice@acme.com", "Re: Lunch?", "th-1", "<a-1@acme.com>")]
    # Both messages labelled; neither has UNREAD removed.
    assert {mid for mid, _ in svc.messages_api.modified} == {"m1", "m2"}
    for _mid, body in svc.messages_api.modified:
        assert body == {"addLabelIds": ["Label_9"]}
        assert "removeLabelIds" not in body


class _ReconSvc:
    def __init__(self, draft_ids, threads):
        self._draft_ids = set(draft_ids)
        self._threads = threads  # thread_id -> list of message label-lists

    def users(self):
        return self

    def drafts(self):
        return self

    def threads(self):
        return self

    def list(self, *, userId):  # noqa: N803  (drafts().list)
        return _Exec({"drafts": [{"id": i} for i in self._draft_ids]})

    def get(self, *, userId, id, format):  # noqa: A002,N803  (threads().get)
        msgs = self._threads.get(id, [])
        return _Exec({"messages": [{"labelIds": labs} for labs in msgs]})


def test_reconcile_undecided_when_draft_still_exists(tmp_path):
    from app.autodraft import reconcile_drafts
    from app.draft_feedback import FileDraftFeedbackStore
    fb = FileDraftFeedbackStore(str(tmp_path / "f.json"))
    fb.track("me", "d1", "alice@acme.com", "t1")
    reconcile_drafts(_ReconSvc({"d1"}, {}), fb, "me", "me")
    assert fb.pending("me") and fb.suppressed("me", "alice@acme.com") is False


def test_reconcile_sent_resets_when_thread_has_sent(tmp_path):
    from app.autodraft import reconcile_drafts
    from app.draft_feedback import FileDraftFeedbackStore
    fb = FileDraftFeedbackStore(str(tmp_path / "f.json"))
    fb.track("me", "d1", "alice@acme.com", "t1")
    fb.record_deletion("me", "alice@acme.com")
    fb.record_deletion("me", "alice@acme.com")  # suppressed
    reconcile_drafts(_ReconSvc(set(), {"t1": [["INBOX"], ["SENT"]]}), fb, "me", "me")
    assert fb.suppressed("me", "alice@acme.com") is False and fb.pending("me") == {}


def test_reconcile_deletion_when_draft_gone_no_sent(tmp_path):
    from app.autodraft import reconcile_drafts
    from app.draft_feedback import FileDraftFeedbackStore
    fb = FileDraftFeedbackStore(str(tmp_path / "f.json"))
    fb.track("me", "d1", "alice@acme.com", "t1")
    fb.record_deletion("me", "alice@acme.com")  # already 1
    reconcile_drafts(_ReconSvc(set(), {"t1": [["INBOX"]]}), fb, "me", "me")
    assert fb.suppressed("me", "alice@acme.com") is True and fb.pending("me") == {}


def _command_msg(mid, actions_text, sender="alice@acme.com", refs="<orig-1@x>"):
    return _msg(
        mid,
        {"From": "me@x.com", "To": "me+nora@x.com", "Subject": "Fwd: Hello", "References": refs},
        body_text=(f"{actions_text}\n---------- Forwarded message ----------\n"
                   f"From: {sender}\nSubject: Hello\nHi"),
    )


def _run(svc, prov, rules, **kw):
    return autodraft_once(
        svc, user_id="me", label_id="L",
        draft_reply=kw.pop("draft_reply", lambda *a: None),
        save_draft=kw.pop("save_draft", lambda *a, **k: "d"),
        provider=prov, alias="nora", inbox_actions=True, rules=rules, owner="me@x.com", **kw,
    )


def test_command_auto_creates_per_sender_rule():
    from app.inbox_rules import FileInboxRuleStore
    import tempfile
    import os
    path = os.path.join(tempfile.mkdtemp(), "r.json")
    rules = FileInboxRuleStore(path)
    _run(_FakeService([_command_msg("c1", "star and archive")]), _FakeProvider(), rules)
    assert rules.get("me@x.com", "alice@acme.com") == ["star", "archive"]


def test_command_once_does_not_create_rule():
    from app.inbox_rules import FileInboxRuleStore
    import tempfile
    import os
    rules = FileInboxRuleStore(os.path.join(tempfile.mkdtemp(), "r.json"))
    _run(_FakeService([_command_msg("c1", "star and archive, once")]), _FakeProvider(), rules)
    assert rules.get("me@x.com", "alice@acme.com") is None


def test_command_stop_removes_rule():
    from app.inbox_rules import FileInboxRuleStore
    import tempfile
    import os
    rules = FileInboxRuleStore(os.path.join(tempfile.mkdtemp(), "r.json"))
    rules.set("me@x.com", "alice@acme.com", ["archive"])
    _run(_FakeService([_command_msg("c1", "stop")]), _FakeProvider(), rules)
    assert rules.get("me@x.com", "alice@acme.com") is None


def test_command_rules_query_drafts_readback():
    from app.inbox_rules import FileInboxRuleStore
    import tempfile
    import os
    rules = FileInboxRuleStore(os.path.join(tempfile.mkdtemp(), "r.json"))
    rules.set("me@x.com", "alice@acme.com", ["archive"])
    saved = []
    _run(_FakeService([_command_msg("c1", "rules?")]), _FakeProvider(), rules,
         save_draft=lambda to, subj, body, **k: saved.append((to, body)) or "d")
    assert saved and "alice@acme.com: archive" in saved[0][1]


def test_archive_rule_applies_actions_and_suppresses_draft():
    from app.inbox_rules import FileInboxRuleStore
    import tempfile
    import os
    rules = FileInboxRuleStore(os.path.join(tempfile.mkdtemp(), "r.json"))
    rules.set("me@x.com", "alice@acme.com", ["star", "archive"])
    incoming = _msg("i1", {"From": "Alice <alice@acme.com>", "Subject": "Hi"})
    svc, prov, drafted = _FakeService([incoming]), _FakeProvider(), []
    n = _run(svc, prov, rules, draft_reply=lambda *a: drafted.append(a) or object(),
             filed_label_id="Label_filed")
    assert prov.applied == [("i1", ["star", "archive"])]  # rule applied to the incoming message
    assert drafted == [] and n == 0                       # archive → no draft
    # labelled both Nora/filed and the idempotency label
    labels = [b for _mid, b in svc.messages_api.modified]
    assert {"addLabelIds": ["Label_filed"]} in labels and {"addLabelIds": ["L"]} in labels


def test_star_only_rule_still_drafts():
    from app.inbox_rules import FileInboxRuleStore
    import tempfile
    import os
    rules = FileInboxRuleStore(os.path.join(tempfile.mkdtemp(), "r.json"))
    rules.set("me@x.com", "alice@acme.com", ["star"])
    incoming = _msg("i1", {"From": "alice@acme.com", "Subject": "Hi"})
    from app.models import NoraResult, ResultKind
    svc, prov = _FakeService([incoming]), _FakeProvider()
    n = _run(svc, prov, rules,
             draft_reply=lambda *a: NoraResult(kind=ResultKind.DRAFT, text="D", interpretation_readback=""))
    assert prov.applied == [("i1", ["star"])] and n == 1  # star kept in inbox → still drafts


def test_suppressed_sender_is_not_drafted():
    from app.draft_feedback import FileDraftFeedbackStore
    import tempfile
    import os
    fb = FileDraftFeedbackStore(os.path.join(tempfile.mkdtemp(), "f.json"))
    fb.record_deletion("me@x.com", "alice@acme.com")
    fb.record_deletion("me@x.com", "alice@acme.com")  # suppressed
    incoming = _msg("i1", {"From": "Alice <alice@acme.com>", "Subject": "Hi"})
    drafted = []
    n = autodraft_once(
        _FakeService([incoming]), user_id="me", label_id="L",
        draft_reply=lambda *a: drafted.append(a) or object(),
        save_draft=lambda *a, **k: "d", owner="me@x.com",
        feedback=fb, draft_learning=True,
    )
    assert drafted == [] and n == 0  # suppressed sender → no draft


def test_created_draft_is_tracked():
    from app.draft_feedback import FileDraftFeedbackStore
    from app.models import NoraResult, ResultKind
    import tempfile
    import os
    fb = FileDraftFeedbackStore(os.path.join(tempfile.mkdtemp(), "f.json"))
    incoming = _msg("i1", {"From": "alice@acme.com", "Subject": "Hi"}, thread_id="th-i1")
    n = autodraft_once(
        _FakeService([incoming]), user_id="me", label_id="L",
        draft_reply=lambda *a: NoraResult(kind=ResultKind.DRAFT, text="D", interpretation_readback=""),
        save_draft=lambda *a, **k: "draft-9", owner="me@x.com",
        feedback=fb, draft_learning=True,
    )
    assert n == 1
    assert fb.pending("me@x.com") == {"draft-9": ("alice@acme.com", "th-i1")}


def test_autodraft_once_skips_save_when_draft_reply_returns_none():
    personal = _msg("m1", {"From": "alice@acme.com", "Subject": "Hi"})
    svc = _FakeService([personal])
    saved = []
    n = autodraft_once(
        svc, user_id="me", label_id="L",
        draft_reply=lambda s, sub, b: None,  # e.g. non-R1 sender
        save_draft=lambda *a, **k: saved.append(a) or "x",
    )
    assert n == 0 and saved == []
    assert svc.messages_api.modified == [("m1", {"addLabelIds": ["L"]})]  # still labelled


def _h(**kw):
    return {k.replace("_", "-"): v for k, v in kw.items()}


def test_genuine_person_to_person_passes():
    assert is_personal_inbound(_h(From="Alice <alice@acme.com>", Subject="Lunch?")) is True


def test_mailing_list_markers_skipped():
    assert is_personal_inbound(_h(From="a@x.com", List_Unsubscribe="<mailto:u@x.com>")) is False
    assert is_personal_inbound(_h(From="a@x.com", List_Id="news.x.com")) is False


def test_precedence_bulk_list_junk_skipped():
    for p in ("bulk", "list", "junk", "BULK"):
        assert is_personal_inbound(_h(From="a@x.com", Precedence=p)) is False


def test_auto_submitted_skipped_unless_no():
    assert is_personal_inbound(_h(From="a@x.com", Auto_Submitted="auto-replied")) is False
    assert is_personal_inbound(_h(From="a@x.com", Auto_Submitted="auto-generated")) is False
    assert is_personal_inbound(_h(From="a@x.com", Auto_Submitted="no")) is True


def test_non_human_senders_skipped():
    for addr in ("no-reply@x.com", "noreply@x.com", "donotreply@x.com",
                 "do-not-reply@x.com", "mailer-daemon@x.com", "postmaster@x.com"):
        assert is_personal_inbound(_h(From=f"Robot <{addr}>")) is False


def test_header_keys_are_case_insensitive():
    assert is_personal_inbound({"from": "a@x.com", "LIST-UNSUBSCRIBE": "x"}) is False
