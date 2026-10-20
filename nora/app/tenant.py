"""R2 in-tenant draft seam. When a relationship is handled at R2, the reply is saved to
the CLIENT's own Drafts folder (mail stays in their tenant) rather than sent. The provider
is pluggable; the default NullTenantDraftClient declines, so R2 fails closed (no processing
on R1 infra) until a real tenant backend is wired. A real client uses the tenant's own
OAuth/Graph/Gmail API — mail never leaves the tenant; only a DPA-covered LLM call may leave."""

from __future__ import annotations

import base64
from email.message import EmailMessage
from typing import Any, Protocol


class TenantUnavailable(RuntimeError):
    """Raised when no tenant backend is configured to save an in-tenant draft."""


class TenantDraftClient(Protocol):
    def save_draft(
        self, to: str, subject: str, body: str, *,
        thread_id: str | None = None, in_reply_to: str | None = None,
    ) -> str:
        """Save a draft in the tenant's Drafts folder; return a draft id/URL. When ``thread_id``
        / ``in_reply_to`` are given, the draft attaches to that conversation (ambient auto-draft).
        Raise TenantUnavailable if no real backend is configured."""
        ...


class NullTenantDraftClient:
    """Default: no tenant backend — R2 relationships decline (no processing on R1)."""

    def save_draft(
        self, to: str, subject: str, body: str, *,
        thread_id: str | None = None, in_reply_to: str | None = None,
    ) -> str:
        raise TenantUnavailable("tenant backend not configured")


class GmailDraftTenantClient:
    """Saves the reply as a draft in the client's own Gmail Drafts folder (mail stays in
    their tenant). ``service`` is a Gmail API resource (injected in tests), normally built
    via a domain-wide-delegated service account impersonating the tenant mailbox. Nora
    never sends here — it only writes a draft the client sends themselves (R2 = drafts-only
    inside the tenant)."""

    def __init__(self, user_id: str, *, service: Any) -> None:
        self._user_id = user_id or "me"
        self._service = service

    def save_draft(
        self, to: str, subject: str, body: str, *,
        thread_id: str | None = None, in_reply_to: str | None = None,
    ) -> str:
        msg = EmailMessage()
        if to:
            msg["To"] = to
        msg["Subject"] = subject
        if in_reply_to:  # thread the draft onto the original conversation
            msg["In-Reply-To"] = in_reply_to
            msg["References"] = in_reply_to
        msg.set_content(body)
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        message: dict[str, Any] = {"raw": raw}
        if thread_id:
            message["threadId"] = thread_id
        created = (
            self._service.users()
            .drafts()
            .create(userId=self._user_id, body={"message": message})
            .execute()
        )
        return str(created.get("id", ""))


class GraphDraftTenantClient:
    """Saves the reply as a draft in the client's own Microsoft 365 mailbox (Outlook
    Drafts) via Microsoft Graph. ``session`` is an httpx-like client carrying an app-only
    bearer token (injected in tests). Creating a message (POST /messages) yields a DRAFT;
    Nora never calls /sendMail — it only ever drafts. NOTE: Graph has no draft-only scope,
    so the app registration uses Mail.ReadWrite (broader than Gmail's compose-only) —
    documented in the R2 onboarding runbook."""

    def __init__(
        self, tenant_user: str, *, session: Any, base_url: str = "https://graph.microsoft.com/v1.0"
    ) -> None:
        self._user = tenant_user
        self._session = session
        self._base = base_url.rstrip("/")

    def save_draft(
        self, to: str, subject: str, body: str, *,
        thread_id: str | None = None, in_reply_to: str | None = None,
    ) -> str:
        # Graph threading (conversationId/replyTo) is deferred — accept the params for a uniform
        # interface; a standalone draft is created regardless.
        payload = {
            "subject": subject,
            "body": {"contentType": "Text", "content": body},
            "toRecipients": [{"emailAddress": {"address": to}}] if to else [],
        }
        resp = self._session.post(f"{self._base}/users/{self._user}/messages", json=payload)
        resp.raise_for_status()
        return str(resp.json().get("id", ""))


def build_graph_session(tenant_id: str, client_id: str, client_secret: str, *, timeout: float = 10.0) -> Any:
    """Build an httpx client with an app-only (client-credentials) Graph token. Lazy
    import so httpx/Graph aren't required unless this backend is selected."""
    import httpx

    token_resp = httpx.post(
        f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token",
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "scope": "https://graph.microsoft.com/.default",
            "grant_type": "client_credentials",
        },
        timeout=timeout,
    )
    token_resp.raise_for_status()
    token = token_resp.json()["access_token"]
    return httpx.Client(headers={"Authorization": f"Bearer {token}"}, timeout=timeout)


def build_gmail_service(credentials_json: str, tenant_user: str) -> Any:
    """Build a Gmail API service from a domain-wide-delegated service-account key,
    impersonating ``tenant_user`` (the client mailbox). Lazy import so the google libs
    aren't required unless this backend is selected. Scope is compose-only (draft, never send)."""
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    creds = service_account.Credentials.from_service_account_file(
        credentials_json, scopes=["https://www.googleapis.com/auth/gmail.compose"], subject=tenant_user
    )
    return build("gmail", "v1", credentials=creds, cache_discovery=False)
