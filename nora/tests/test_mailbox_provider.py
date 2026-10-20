from app.mailbox.provider import GmailMailboxProvider, NullMailboxProvider


class _Exec:
    def __init__(self, r):
        self._r = r

    def execute(self):
        return self._r


class _Labels:
    def __init__(self, existing):
        self.labels = list(existing)
        self.created = []

    def list(self, *, userId):  # noqa: N803
        return _Exec({"labels": self.labels})

    def create(self, *, userId, body):  # noqa: N803
        nl = {"id": "Label_" + body["name"], "name": body["name"]}
        self.labels.append(nl)
        self.created.append(body["name"])
        return _Exec(nl)


class _Msgs:
    def __init__(self):
        self.modified = None
        self.listed = None
        self._hits = [{"id": "found1"}]

    def modify(self, *, userId, id, body):  # noqa: A002,N803
        self.modified = {"id": id, "body": body}
        return _Exec({})

    def list(self, *, userId, q, maxResults):  # noqa: N803
        self.listed = q
        return _Exec({"messages": self._hits})


class _Svc:
    def __init__(self, labels=()):
        self._l = _Labels(labels)
        self._m = _Msgs()

    def users(self):
        return self

    def labels(self):
        return self._l

    def messages(self):
        return self._m


def test_apply_actions_maps_to_label_ops():
    svc = _Svc(labels=[{"id": "Label_PX", "name": "ProjectX"}])
    GmailMailboxProvider(svc).apply_actions("m1", ["star", "archive", "read", "important", "label:ProjectX"])
    assert svc._m.modified["id"] == "m1"
    body = svc._m.modified["body"]
    assert set(body["addLabelIds"]) == {"STARRED", "IMPORTANT", "Label_PX"}
    assert set(body["removeLabelIds"]) == {"INBOX", "UNREAD"}


def test_apply_actions_creates_missing_label():
    svc = _Svc(labels=[])
    GmailMailboxProvider(svc).apply_actions("m1", ["label:NewOne"])
    assert "NewOne" in svc._l.created
    assert svc._m.modified["body"]["addLabelIds"] == ["Label_NewOne"]


def test_unread_adds_unread():
    svc = _Svc()
    GmailMailboxProvider(svc).apply_actions("m1", ["unread"])
    assert svc._m.modified["body"]["addLabelIds"] == ["UNREAD"]


def test_apply_actions_noop_when_empty():
    svc = _Svc()
    GmailMailboxProvider(svc).apply_actions("m1", [])
    assert svc._m.modified is None  # no API call for nothing to do


def test_find_message_by_msgid():
    svc = _Svc()
    assert GmailMailboxProvider(svc).find_message("a@x.com", "Subj", "<mid-1@x>") == "found1"
    assert "rfc822msgid:<mid-1@x>" in svc._m.listed


def test_find_message_by_from_subject_strips_prefix():
    svc = _Svc()
    GmailMailboxProvider(svc).find_message("a@x.com", "Fwd: Hello", None)
    assert "from:a@x.com" in svc._m.listed and 'subject:"Hello"' in svc._m.listed


def test_find_message_none_when_no_hits():
    svc = _Svc()
    svc._m._hits = []
    assert GmailMailboxProvider(svc).find_message("a@x.com", "b", None) is None


def test_null_provider_noops():
    n = NullMailboxProvider()
    n.apply_actions("m1", ["star"])  # no error
    assert n.find_message("a", "b", None) is None
