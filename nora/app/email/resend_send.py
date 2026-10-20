"""Resend outbound send client.

Implements the ``EmailProviderClient`` send seam via a direct ``httpx`` POST to
the Resend email API. Sends from the brand domain with domain DKIM (the domain is
verified in Resend via DNS records, which coexist with Cloudflare Email Routing's
MX). The From address is per-person — passed in as ``sender`` (e.g.
``democeo@nora-bennett.com``) — with a configured default as fallback.

A non-2xx response or transport error raises, so the pipeline's background-task
handler degrades to a failure receipt rather than silence (same contract as the
other send clients).
"""

from __future__ import annotations

import base64

import httpx

from app.models import OutboundAttachment

_RESEND_EMAIL_URL = "https://api.resend.com/emails"


class ResendSendClient:
    """Sends mail through Resend using an API key + a verified sending domain."""

    # Resend honors the Idempotency-Key header → a boot-replay can't double-send.
    supports_idempotency = True

    def __init__(
        self,
        api_key: str,
        default_sender: str,
        *,
        client: httpx.Client | None = None,
        timeout: float = 10.0,
    ) -> None:
        self._default_sender = default_sender
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        self._client = client or httpx.Client(timeout=timeout)

    def send(
        self,
        *,
        to: str,
        subject: str,
        body: str,
        reply_to: str | None,
        sender: str | None = None,
        idempotency_key: str | None = None,
        attachments: list[OutboundAttachment] | None = None,
    ) -> None:
        payload: dict[str, object] = {
            "from": sender or self._default_sender,
            "to": [to],
            "subject": subject,
            "text": body,
        }
        if reply_to:
            payload["reply_to"] = reply_to
        if attachments:
            payload["attachments"] = [
                {
                    "filename": a.filename,
                    "content": base64.b64encode(a.content).decode(),
                    "content_type": a.content_type,
                }
                for a in attachments
            ]
        headers = self._headers
        if idempotency_key:
            # Resend dedupes sends carrying the same Idempotency-Key — defends the
            # consume-before-send guarantee against a transport-level retry (KTD-H).
            headers = {**self._headers, "Idempotency-Key": idempotency_key}
        response = self._client.post(_RESEND_EMAIL_URL, json=payload, headers=headers)
        response.raise_for_status()
