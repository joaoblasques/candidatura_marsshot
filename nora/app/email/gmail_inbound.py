"""Showcase Gmail-pull inbound. Adapts a Gmail API message into Nora's normalized
``InboundEmail`` and polls the inbox. DKIM comes from Gmail's own stamped
``Authentication-Results`` (the trusted authserv-id), mirroring the Cloudflare-worker path.
Fail-closed: no author-aligned DKIM pass -> ``dkim_verified=False``.
"""

from __future__ import annotations

import base64
from typing import Any, Callable

from app.email.inbound import split_secret_address
from app.email.postmark_inbound import _author_domain, _authentication_results_pass
from app.models import Attachment, InboundEmail
from app.security import ReplayGuard


def _header(headers: list[dict], name: str) -> str:
    for h in headers:
        if str(h.get("name", "")).lower() == name.lower():
            return str(h.get("value", ""))
    return ""


def _from_address(value: str) -> str:
    """"Sarah <sarah@acme.com>" -> "sarah@acme.com"; "x@acme.com" -> "x@acme.com"."""
    v = value.strip()
    if "<" in v and ">" in v:
        return v[v.rfind("<") + 1 : v.rfind(">")].strip().lower()
    return v.lower()


def _plain_text_body(payload: dict) -> str:
    for p in payload.get("parts") or []:
        if p.get("mimeType") == "text/plain":
            data = (p.get("body") or {}).get("data") or ""
            if data:
                return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
    data = (payload.get("body") or {}).get("data") or ""
    return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace") if data else ""


def _attachments(payload: dict) -> list[Attachment]:
    out: list[Attachment] = []
    for p in payload.get("parts") or []:
        filename = p.get("filename") or ""
        body = p.get("body") or {}
        if filename and body.get("attachmentId"):
            out.append(
                Attachment(filename=filename, content_type=p.get("mimeType"), size=body.get("size"))
            )
    return out


def adapt_gmail_message(msg: dict, *, trusted_authserv_id: str) -> InboundEmail:
    payload = msg.get("payload") or {}
    headers = payload.get("headers") or []
    from_addr = _from_address(_header(headers, "From"))
    ar = _header(headers, "Authentication-Results")
    dkim = bool(trusted_authserv_id) and _authentication_results_pass(ar, _author_domain(from_addr))
    # Decode the plus-addressed reply-to (nora+TOKEN@domain) so a reply to one of Nora's
    # correlation addresses recovers both the base local-part and the token — this is what
    # lets the consent gate / revise loop match an AG-confirm to its pending proposal.
    secret_local, correlation_token = split_secret_address(_from_address(_header(headers, "To")))
    return InboundEmail(
        event_id=str(msg.get("id") or ""),
        secret_address_local=secret_local,
        correlation_token=correlation_token,
        from_address=from_addr,
        dkim_verified=dkim,
        subject=_header(headers, "Subject"),
        body=_plain_text_body(payload),
        attachments=_attachments(payload),
    )


class GmailInboundPoller:
    """Polls a Gmail inbox for unread mail, dispatches each (deduped) to ``on_message`` as an
    InboundEmail, then marks it read. ``service`` is a Gmail API resource (injected)."""

    def __init__(
        self, service: Any, on_message: Callable[[InboundEmail], None], *,
        trusted_authserv_id: str, user_id: str = "me",
    ) -> None:
        self._service = service
        self._on_message = on_message
        self._authserv = trusted_authserv_id
        self._user = user_id
        self._seen = ReplayGuard(window_seconds=86_400)

    def poll_once(self) -> int:
        listing = (
            self._service.users().messages()
            .list(userId=self._user, q="is:unread", maxResults=25).execute()
        )
        dispatched = 0
        for ref in listing.get("messages") or []:
            mid = ref.get("id")
            if not mid or self._seen.is_replay(mid):
                continue
            msg = (
                self._service.users().messages()
                .get(userId=self._user, id=mid, format="full").execute()
            )
            self._on_message(adapt_gmail_message(msg, trusted_authserv_id=self._authserv))
            self._service.users().messages().modify(
                userId=self._user, id=mid, body={"removeLabelIds": ["UNREAD"]}
            ).execute()
            dispatched += 1
        return dispatched


def build_gmail_read_service(credentials_json: str, tenant_user: str) -> Any:
    """Gmail service with modify+send scopes, domain-wide-delegated to the showcase mailbox.
    Lazy import so the google libs aren't required unless the showcase runs."""
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    creds = service_account.Credentials.from_service_account_file(
        credentials_json,
        scopes=[
            "https://www.googleapis.com/auth/gmail.modify",
            "https://www.googleapis.com/auth/gmail.send",
        ],
        subject=tenant_user,
    )
    return build("gmail", "v1", credentials=creds, cache_discovery=False)
