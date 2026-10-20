"""_build_send_client transport selection — regression: gmail+oauth must not touch the
service-account builder (empty in OAuth mode → the ambient auto-draft poller crash-looped)."""

from __future__ import annotations

import app.email.gmail_inbound as gi
import app.email.gmail_oauth as go
from app.config import Settings
from app.main import _build_send_client


def test_gmail_oauth_send_client_uses_oauth_not_service_account(monkeypatch):
    seen = {}
    monkeypatch.setattr(go, "build_gmail_oauth_service",
                        lambda token: seen.setdefault("oauth_token", token) or object())

    def _boom(*a, **k):
        raise AssertionError("service-account builder must not run in oauth mode")

    monkeypatch.setattr(gi, "build_gmail_read_service", _boom)

    s = Settings(transport="gmail", gmail_auth="oauth",
                 gmail_oauth_token="/tok.json", gmail_tenant_user="me@gmail.com")
    client = _build_send_client(s)  # no service-account key set — must not raise
    assert seen["oauth_token"] == "/tok.json"
    assert client is not None
