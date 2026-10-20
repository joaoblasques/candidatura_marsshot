"""Outbound send (U6).

CEO-only by default (R1/KTD7): ``send_to_ceo`` is the only unconditional send. As of
0.2.0 there is exactly ONE consent-gated exception — ``send_on_behalf`` — used solely
to deliver a meeting proposal the CEO explicitly confirmed (AG), from Nora's own
labeled address. It is reachable only via a confirmed action descriptor (see
app/pipeline.py ``_handle_confirm`` and app/main.py ``_execute_action``); there is no
path to it from drafting, revise, or a fresh request.
"""

from __future__ import annotations

from typing import Protocol

from app.models import OutboundAttachment
from app.receipt import ComposedReply

# Appended to every Nora-sent email (CEO receipts AND the consent-gated third-party send), at
# the send layer so it lands on the OUTER message only — never inside a draft the CEO copies to
# send (the draft sits above this footer). Stored bodies (outbox) stay clean; the footer is added
# once per actual send, so idempotent replays are unaffected.
SIGNATURE = (
    "\n\n-- \n"
    "Nora Bennett — an email-native AI executive assistant. She drafts replies, summaries, and\n"
    "reports, and acts (meetings, shares) only with your approval — never sending as you.\n"
    "https://docs.nora-bennett.com/"
)


def _with_signature(body: str) -> str:
    """Append the Nora signature unless it's already present (idempotent)."""
    body = body or ""
    return body if SIGNATURE.strip() in body else body + SIGNATURE


class EmailProviderClient(Protocol):
    """The send seam a provider adapter must implement (KTD2).

    ``sender`` optionally overrides the From address (used for per-person
    domain-DKIM From); when None the client uses its configured default.
    ``idempotency_key`` (when supported) lets the provider dedupe a retried send.
    ``attachments`` (when present) are files to attach to the message.
    """

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
    ) -> None: ...


class OutboundSender:
    """Sends Nora's reply to the CEO, plus the one consent-gated third-party send."""

    def __init__(self, client: EmailProviderClient) -> None:
        self._client = client

    @property
    def supports_idempotency(self) -> bool:
        """Whether the transport dedupes a retried send (so a boot-replay is safe)."""
        return bool(getattr(self._client, "supports_idempotency", False))

    def send_to_ceo(
        self,
        ceo_address: str,
        reply: ComposedReply,
        reply_to: str | None = None,
        sender: str | None = None,
    ) -> None:
        """Send the composed reply to the verified CEO address.

        ``reply_to`` carries the plus-addressed correlation address (U7/KTD6) so
        the CEO's revise-reply routes back correlated. ``sender`` optionally sets
        the From (the person's own domain address for domain-DKIM transports).
        """
        self._client.send(
            to=ceo_address,
            subject=reply.subject,
            body=_with_signature(reply.body),
            reply_to=reply_to,
            sender=sender,
            attachments=reply.attachments or None,
        )

    def send_on_behalf(
        self,
        recipient: str,
        reply: ComposedReply,
        *,
        sender: str,
        reply_to: str | None,
        idempotency_key: str | None = None,
        attachments: list[OutboundAttachment] | None = None,
    ) -> None:
        """Consent-gated send to a third party (0.2.0). ``sender`` MUST be Nora's own
        labeled address (never the CEO's) and is required — an empty From fails closed
        rather than risking an unlabeled/wrong send. ``reply_to`` is the CEO's address
        so the third party's reply routes to the CEO. ``attachments`` carry a
        consent-gated shared file. Only ever called from the confirmed-action path."""
        if not sender:
            raise ValueError("send_on_behalf requires a non-empty sender (Nora's address)")
        self._client.send(
            to=recipient,
            subject=reply.subject,
            body=_with_signature(reply.body),
            reply_to=reply_to,
            sender=sender,
            idempotency_key=idempotency_key,
            attachments=attachments,
        )
