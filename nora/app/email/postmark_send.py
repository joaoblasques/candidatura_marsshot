"""Postmark outbound send client (U3).

Implements the ``EmailProviderClient`` send seam via a direct ``httpx`` POST to
the Postmark email API (KTD5 — there is no maintained first-party Python SDK).
``From`` is the verified sender signature; ``ReplyTo`` carries the plus-addressed
correlation address so the CEO's revise-reply routes back correlated. CEO-only is
preserved by ``OutboundSender`` — this client only sends where it is told.

A non-2xx response or transport error raises, so the pipeline's background-task
handler degrades to a failure receipt rather than silence.
"""

from __future__ import annotations

import base64

import httpx

from app.models import OutboundAttachment

_POSTMARK_EMAIL_URL = "https://api.postmarkapp.com/email"


class PostmarkSendClient:
    """Sends mail through Postmark using a server token + verified sender."""

    # Postmark's send API ignores our idempotency key → a boot-replay could re-send.
    supports_idempotency = False

    def __init__(
        self,
        server_token: str,
        sender: str,
        *,
        client: httpx.Client | None = None,
        timeout: float = 10.0,
    ) -> None:
        self._sender = sender
        self._headers = {
            "X-Postmark-Server-Token": server_token,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        # An injected client (e.g. a MockTransport) makes this unit testable
        # without network access.
        self._client = client or httpx.Client(timeout=timeout)

    def send(
        self,
        *,
        to: str,
        subject: str,
        body: str,
        reply_to: str | None,
        sender: str | None = None,
        idempotency_key: str | None = None,  # accepted for protocol parity; unused
        attachments: list[OutboundAttachment] | None = None,
    ) -> None:
        payload: dict[str, object] = {
            "From": sender or self._sender,
            "To": to,
            "Subject": subject,
            "TextBody": body,
        }
        if reply_to:
            payload["ReplyTo"] = reply_to
        if attachments:
            payload["Attachments"] = [
                {
                    "Name": a.filename,
                    "Content": base64.b64encode(a.content).decode(),
                    "ContentType": a.content_type,
                }
                for a in attachments
            ]
        response = self._client.post(_POSTMARK_EMAIL_URL, json=payload, headers=self._headers)
        response.raise_for_status()
