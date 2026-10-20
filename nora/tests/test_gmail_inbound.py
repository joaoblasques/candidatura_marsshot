"""Showcase Gmail-pull inbound: adapter + poller."""

from __future__ import annotations

import base64

from app.email.gmail_inbound import adapt_gmail_message


def _b64url(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode()


def _msg(headers, body_text, msg_id="gmail-1"):
    return {
        "id": msg_id,
        "payload": {
            "headers": [{"name": k, "value": v} for k, v in headers.items()],
            "mimeType": "text/plain",
            "body": {"data": _b64url(body_text)},
        },
    }


def test_adapt_maps_core_fields_and_dkim():
    ar = "mx.google.com; dkim=pass header.d=acme.com; spf=pass; dmarc=pass"
    msg = _msg(
        {"From": "Sarah <sarah@acme.com>", "Subject": "Q3", "To": "nora@demo.nora-bennett.com",
         "Authentication-Results": ar},
        "WIYOW please reply",
    )
    email = adapt_gmail_message(msg, trusted_authserv_id="mx.google.com")
    assert email.event_id == "gmail-1"
    assert email.from_address == "sarah@acme.com"
    assert email.subject == "Q3"
    assert email.body == "WIYOW please reply"
    assert email.dkim_verified is True


def test_adapt_dkim_false_without_aligned_pass():
    msg = _msg({"From": "x@acme.com", "Subject": "s", "Authentication-Results":
                "mx.google.com; dkim=pass header.d=evil.com"}, "hi")
    assert adapt_gmail_message(msg, trusted_authserv_id="mx.google.com").dkim_verified is False


def test_adapt_decodes_plus_addressed_correlation_token():
    # A reply to Nora's plus-addressed Reply-To (nora+TOKEN@domain) must split into the
    # base local-part + correlation token so the consent gate / revise loop can match it.
    # Without this the AG-confirm of a meeting proposal never correlates (Beat 2 breaks).
    msg = _msg({"From": "exec@demo.nora-bennett.com", "Subject": "Re: proposal",
                "To": "nora+abc123tok@demo.nora-bennett.com",
                "Authentication-Results":
                "mx.google.com; dkim=pass header.d=demo.nora-bennett.com"}, "AG")
    email = adapt_gmail_message(msg, trusted_authserv_id="mx.google.com")
    assert email.secret_address_local == "nora"
    assert email.correlation_token == "abc123tok"


def test_adapt_bare_address_has_no_correlation_token():
    msg = _msg({"From": "exec@demo.nora-bennett.com", "Subject": "s",
                "To": "nora@demo.nora-bennett.com",
                "Authentication-Results":
                "mx.google.com; dkim=pass header.d=demo.nora-bennett.com"}, "hi")
    email = adapt_gmail_message(msg, trusted_authserv_id="mx.google.com")
    assert email.secret_address_local == "nora"
    assert email.correlation_token is None


from app.email.gmail_inbound import GmailInboundPoller  # noqa: E402


class _Exec:
    def __init__(self, r): self._r = r
    def execute(self): return self._r


class _FakeMessages:
    def __init__(self, sink, listing, msgs):
        self._sink, self._listing, self._msgs = sink, listing, msgs

    def list(self, *, userId, q, maxResults):  # noqa: N803
        self._sink["q"] = q
        return _Exec({"messages": self._listing})

    def get(self, *, userId, id, format):  # noqa: N803, A002
        return _Exec(self._msgs[id])

    def modify(self, *, userId, id, body):  # noqa: N803
        self._sink.setdefault("read", []).append(id)
        return _Exec({})


class _FakeGmail:
    def __init__(self, listing, msgs):
        self.sink: dict = {}
        self._m = _FakeMessages(self.sink, listing, msgs)

    def users(self):
        outer = self
        class _U:
            def messages(self_inner):
                return outer._m
        return _U()


def _ok_msg(mid):
    return _msg({"From": "s@acme.com", "Subject": "x",
                 "Authentication-Results": "mx.google.com; dkim=pass header.d=acme.com"}, "hi", mid)


def test_poll_dispatches_unread_and_marks_read():
    svc = _FakeGmail([{"id": "m1"}], {"m1": _ok_msg("m1")})
    seen = []
    poller = GmailInboundPoller(svc, seen.append, trusted_authserv_id="mx.google.com")
    n = poller.poll_once()
    assert n == 1 and seen[0].event_id == "m1"
    assert svc.sink["read"] == ["m1"]
    assert "is:unread" in svc.sink["q"]


def test_poll_dedupes_already_seen():
    svc = _FakeGmail([{"id": "m1"}], {"m1": _ok_msg("m1")})
    seen = []
    poller = GmailInboundPoller(svc, seen.append, trusted_authserv_id="mx.google.com")
    poller.poll_once()
    poller.poll_once()
    assert len(seen) == 1
