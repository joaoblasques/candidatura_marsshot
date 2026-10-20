"""Outlook / Microsoft Graph mailbox provider — canonical actions → Graph primitives, and message
lookup via $filter/$search. The session is a fake httpx-like client that records calls."""

from app.mailbox.provider import GraphMailboxProvider

_BASE = "https://graph.microsoft.com/v1.0"
_U = "nora@contoso.com"


class _Resp:
    def __init__(self, data=None):
        self._data = data or {}

    def raise_for_status(self):
        return None

    def json(self):
        return self._data


class _FakeSession:
    def __init__(self, get_payload=None):
        self.calls = []  # list of (method, url, json)
        self._get_payload = get_payload or {}

    def patch(self, url, json=None):
        self.calls.append(("PATCH", url, json))
        return _Resp()

    def post(self, url, json=None):
        self.calls.append(("POST", url, json))
        return _Resp()

    def get(self, url):
        self.calls.append(("GET", url, None))
        return _Resp(self._get_payload)


def _provider(session):
    return GraphMailboxProvider(_U, session=session, base_url=_BASE)


def test_star_read_important_map_to_one_patch():
    s = _FakeSession()
    _provider(s).apply_actions("m1", ["star", "read", "important"])
    patches = [c for c in s.calls if c[0] == "PATCH"]
    assert len(patches) == 1
    body = patches[0][2]
    assert body == {"isRead": True, "importance": "high", "flag": {"flagStatus": "flagged"}}


def test_archive_is_a_move_to_the_archive_folder():
    s = _FakeSession()
    _provider(s).apply_actions("m1", ["archive"])
    posts = [c for c in s.calls if c[0] == "POST"]
    assert posts == [("POST", f"{_BASE}/users/{_U}/messages/m1/move", {"destinationId": "archive"})]


def test_label_unions_with_existing_categories():
    s = _FakeSession(get_payload={"categories": ["Keep"]})
    _provider(s).apply_actions("m1", ["label:Clients"])
    body = [c for c in s.calls if c[0] == "PATCH"][0][2]
    assert body == {"categories": ["Keep", "Clients"]}


def test_no_known_actions_is_a_noop():
    s = _FakeSession()
    _provider(s).apply_actions("m1", ["bogus"])
    assert s.calls == []


def test_find_by_message_id_uses_internet_message_id_filter():
    s = _FakeSession(get_payload={"value": [{"id": "abc"}]})
    got = _provider(s).find_message("", "", "<orig-1@x>")
    assert got == "abc"
    url = s.calls[0][1]
    assert "internetMessageId eq '<orig-1@x>'" in url


def test_find_by_sender_subject_strips_fwd_and_searches():
    s = _FakeSession(get_payload={"value": [{"id": "z9"}]})
    got = _provider(s).find_message("alice@acme.com", "Fwd: Q3 plan", None)
    assert got == "z9"
    url = s.calls[0][1]
    assert "$search=" in url and "from:alice@acme.com" in url and "subject:Q3 plan" in url and "Fwd:" not in url


def test_find_returns_none_when_empty():
    s = _FakeSession(get_payload={"value": []})
    assert _provider(s).find_message("a@x.com", "hi", None) is None
