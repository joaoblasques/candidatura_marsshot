from __future__ import annotations
import pytest
from app.tenant import NullTenantDraftClient, TenantUnavailable

def test_null_tenant_declines() -> None:
    with pytest.raises(TenantUnavailable):
        NullTenantDraftClient().save_draft(to="x@y.com", subject="s", body="b")


import base64  # noqa: E402

from app.tenant import GmailDraftTenantClient  # noqa: E402


class _FakeDrafts:
    def __init__(self, sink):
        self._sink = sink

    def create(self, *, userId, body):  # noqa: N803
        self._sink.update(userId=userId, body=body)
        return self

    def execute(self):
        return {"id": "draft-1"}


class _FakeUsers:
    def __init__(self, sink):
        self._sink = sink

    def drafts(self):
        return _FakeDrafts(self._sink)


class _FakeGmail:
    def __init__(self):
        self.sink = {}

    def users(self):
        return _FakeUsers(self.sink)


def test_gmail_client_creates_draft_in_tenant():
    svc = _FakeGmail()
    client = GmailDraftTenantClient("ceo@client.com", service=svc)
    did = client.save_draft(to="sarah@acme.com", subject="Re: Proposal", body="Hi Sarah,")
    assert did == "draft-1"
    assert svc.sink["userId"] == "ceo@client.com"
    # the message is a base64url-encoded MIME message addressed to the recipient
    raw = svc.sink["body"]["message"]["raw"]
    decoded = base64.urlsafe_b64decode(raw).decode()
    assert "To: sarah@acme.com" in decoded
    assert "Subject: Re: Proposal" in decoded
    assert "Hi Sarah," in decoded
    assert "threadId" not in svc.sink["body"]["message"]  # standalone unless threaded


def test_gmail_client_threads_draft_when_given_thread_and_in_reply_to():
    svc = _FakeGmail()
    client = GmailDraftTenantClient("ceo@client.com", service=svc)
    client.save_draft(to="sarah@acme.com", subject="Re: Proposal", body="Hi,",
                      thread_id="thread-9", in_reply_to="<msg-7@acme.com>")
    assert svc.sink["body"]["message"]["threadId"] == "thread-9"
    decoded = base64.urlsafe_b64decode(svc.sink["body"]["message"]["raw"]).decode()
    assert "In-Reply-To: <msg-7@acme.com>" in decoded
    assert "References: <msg-7@acme.com>" in decoded


from app.tenant import GraphDraftTenantClient  # noqa: E402


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeSession:
    def __init__(self):
        self.calls = []

    def post(self, url, json):  # noqa: A002
        self.calls.append((url, json))
        return _FakeResp({"id": "AAMk-draft-1"})


def test_graph_client_creates_draft_via_messages_endpoint():
    sess = _FakeSession()
    client = GraphDraftTenantClient("ceo@client.com", session=sess)
    did = client.save_draft(to="sarah@acme.com", subject="Re: Proposal", body="Hi Sarah,")
    assert did == "AAMk-draft-1"
    url, payload = sess.calls[0]
    assert url.endswith("/users/ceo@client.com/messages")  # creating a message = a draft
    assert payload["subject"] == "Re: Proposal"
    assert payload["body"] == {"contentType": "Text", "content": "Hi Sarah,"}
    assert payload["toRecipients"] == [{"emailAddress": {"address": "sarah@acme.com"}}]
