"""Showcase Gmail send client (sends as the Workspace mailbox via the Gmail API)."""

from __future__ import annotations

import base64
from email import message_from_bytes, policy

from app.email.gmail_send import GmailSendClient
from app.models import OutboundAttachment


class _Exec:
    def __init__(self, r): self._r = r
    def execute(self): return self._r


class _Svc:
    def __init__(self):
        self.sink: dict = {}

    def users(self):
        outer = self
        class _U:
            def messages(self_inner):
                class _M:
                    def send(self_m, *, userId, body):  # noqa: N803
                        outer.sink["raw"] = base64.urlsafe_b64decode(body["raw"])
                        return _Exec({"id": "sent-1"})
                return _M()
        return _U()


def test_gmail_send_builds_mime_with_to_subject_body():
    svc = _Svc()
    GmailSendClient(service=svc, sender="nora@demo.nora-bennett.com").send(
        to="sarah@acme.com", subject="Re: Q3", body="Hi Sarah,", reply_to="ceo@x.com")
    msg = message_from_bytes(svc.sink["raw"], policy=policy.default)
    assert msg["To"] == "sarah@acme.com" and msg["Subject"] == "Re: Q3"
    assert msg["From"] == "nora@demo.nora-bennett.com" and msg["Reply-To"] == "ceo@x.com"
    assert "Hi Sarah," in msg.get_content()


def test_gmail_send_attaches_files():
    svc = _Svc()
    GmailSendClient(service=svc, sender="nora@demo.nora-bennett.com").send(
        to="s@x.com", subject="s", body="b", reply_to=None,
        attachments=[OutboundAttachment(filename="r.docx", content=b"PK", content_type="application/x")])
    msg = message_from_bytes(svc.sink["raw"], policy=policy.default)
    assert any(p.get_filename() == "r.docx" for p in msg.walk())
