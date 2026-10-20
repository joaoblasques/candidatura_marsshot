"""SMTP outbound send client.

Implements the ``EmailProviderClient`` send seam over plain SMTP, so Nora can
deliver replies without Postmark: to **Mailpit** (or any local SMTP sink) for a
self-contained demo, or to a real relay like ``smtp.gmail.com:587`` for a real
round-trip. CEO-only is preserved by ``OutboundSender`` — this client only sends
where it is told.

An SMTP error raises, so the pipeline's background-task handler degrades to a
failure receipt rather than silence (same contract as ``PostmarkSendClient``).
"""

from __future__ import annotations

import smtplib
from collections.abc import Callable
from email.message import EmailMessage

from app.models import OutboundAttachment

# Factory returning an SMTP connection usable as a context manager. Injectable so
# the client is unit-testable without a real server.
SmtpFactory = Callable[[], smtplib.SMTP]


class SmtpSendClient:
    """Sends mail via SMTP (Mailpit locally, or any relay)."""

    # Plain SMTP has no idempotency → a boot-replay could re-send.
    supports_idempotency = False

    def __init__(
        self,
        host: str,
        port: int,
        sender: str,
        *,
        username: str = "",
        password: str = "",
        use_tls: bool = False,
        timeout: float = 10.0,
        smtp_factory: SmtpFactory | None = None,
    ) -> None:
        self._sender = sender
        self._username = username
        self._password = password
        self._use_tls = use_tls
        self._smtp_factory = smtp_factory or (lambda: smtplib.SMTP(host, port, timeout=timeout))

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
        message = EmailMessage()
        message["From"] = sender or self._sender
        message["To"] = to
        message["Subject"] = subject
        if reply_to:
            message["Reply-To"] = reply_to
        message.set_content(body)
        for a in attachments or []:
            maintype, _, subtype = a.content_type.partition("/")
            message.add_attachment(
                a.content,
                maintype=maintype or "application",
                subtype=subtype or "octet-stream",
                filename=a.filename,
            )

        with self._smtp_factory() as smtp:
            if self._use_tls:
                smtp.starttls()
            if self._username:
                smtp.login(self._username, self._password)
            smtp.send_message(message)
