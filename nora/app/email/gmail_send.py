"""Gmail send client (showcase): sends AS the Workspace mailbox via the Gmail API, so
replies/proposals land in Nora's real Sent folder. Implements the EmailProviderClient seam.
"""

from __future__ import annotations

import base64
from email.message import EmailMessage
from typing import Any

from app.models import OutboundAttachment


class GmailSendClient:
    """Sends via the Gmail API as the impersonated Workspace mailbox. ``service`` is a Gmail
    API resource (injected); ``sender`` is the mailbox address (Nora's own labeled identity)."""

    supports_idempotency = False  # the Gmail send API has no idempotency key

    def __init__(self, *, service: Any, sender: str) -> None:
        self._service = service
        self._sender = sender

    def send(
        self, *, to: str, subject: str, body: str, reply_to: str | None,
        sender: str | None = None, idempotency_key: str | None = None,
        attachments: list[OutboundAttachment] | None = None,
    ) -> None:
        msg = EmailMessage()
        msg["From"] = sender or self._sender
        msg["To"] = to
        msg["Subject"] = subject
        if reply_to:
            msg["Reply-To"] = reply_to
        msg.set_content(body)
        for a in attachments or []:
            maintype, _, subtype = a.content_type.partition("/")
            msg.add_attachment(
                a.content, maintype=maintype or "application",
                subtype=subtype or "octet-stream", filename=a.filename,
            )
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        self._service.users().messages().send(userId="me", body={"raw": raw}).execute()
