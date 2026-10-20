"""Local SMTP → webhook gateway for the self-contained demo (DEV ONLY).

Runs a real local SMTP server. Any email sent to it is parsed and POSTed to
Nora's ``/inbound/webhook`` as a **Postmark-compatible payload**, so the demo
exercises the exact production inbound path (the same `adapt_postmark_inbound`
adapter and pipeline). DKIM is asserted locally (dev trust) — never run this in
production; it accepts any sender.

Run order for the demo (see deploy/LOCAL_DEMO.md):
    1. Mailpit          (SMTP sink on :1025, web UI on :8025)
    2. the Nora app     (NORA_TRANSPORT=smtp, NORA_SMTP_HOST=127.0.0.1 …)
    3. this gateway     (listens on :2525, forwards to the app webhook)
    4. scripts/demo_send.py  (sends a forwarded thread into the gateway)

Env:
    NORA_WEBHOOK_URL          default http://127.0.0.1:8099/inbound/webhook
    NORA_WEBHOOK_BASIC_AUTH   default demo:demo  (must match the app)
    NORA_SMTP_GATEWAY_HOST    default 127.0.0.1
    NORA_SMTP_GATEWAY_PORT    default 2525
"""

from __future__ import annotations

import base64
import email
import os
import sys
from email.message import EmailMessage
from email.policy import default as default_policy
from email.utils import parseaddr

import httpx
from aiosmtpd.controller import Controller

WEBHOOK_URL = os.environ.get("NORA_WEBHOOK_URL", "http://127.0.0.1:8099/inbound/webhook")
WEBHOOK_CRED = os.environ.get("NORA_WEBHOOK_BASIC_AUTH", "demo:demo")


def _text_body(msg: EmailMessage) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                return part.get_content()
        return ""
    return msg.get_content()


def to_postmark_payload(msg: EmailMessage, recipient: str) -> dict:
    """Shape a received email like a Postmark inbound webhook payload."""
    local = recipient.split("@", 1)[0]
    mailbox_hash = local.split("+", 1)[1] if "+" in local else ""
    sender = parseaddr(msg.get("From", ""))[1]
    return {
        "MessageID": msg.get("Message-ID") or f"local-{abs(hash(recipient + sender)) }",
        "OriginalRecipient": recipient,
        "FromFull": {"Email": sender},
        "From": sender,
        "Subject": msg.get("Subject", ""),
        "TextBody": _text_body(msg),
        "MailboxHash": mailbox_hash,
        # Dev trust: assert DKIM locally so the production auth gate accepts it.
        "Headers": [
            {"Name": "X-Spam-Tests", "Value": "DKIM_SIGNED,DKIM_VALID,DKIM_VALID_AU,SPF_PASS"}
        ],
        "Attachments": [],
    }


class _Handler:
    async def handle_DATA(self, server, session, envelope):  # noqa: ANN001, ANN201
        msg = email.message_from_bytes(envelope.content, policy=default_policy)
        encoded = base64.b64encode(WEBHOOK_CRED.encode()).decode()
        for recipient in envelope.rcpt_tos:
            payload = to_postmark_payload(msg, recipient)
            try:
                resp = httpx.post(
                    WEBHOOK_URL,
                    json=payload,
                    headers={"Authorization": f"Basic {encoded}"},
                    timeout=15.0,
                )
                print(f">> {recipient}: webhook {resp.status_code}")
            except Exception as exc:  # noqa: BLE001 — surface to the SMTP client
                print(f"!! {recipient}: webhook error {exc}", file=sys.stderr)
                return "451 webhook delivery failed"
        return "250 Message accepted for delivery"


def main() -> None:
    host = os.environ.get("NORA_SMTP_GATEWAY_HOST", "127.0.0.1")
    port = int(os.environ.get("NORA_SMTP_GATEWAY_PORT", "2525"))
    controller = Controller(_Handler(), hostname=host, port=port)
    controller.start()
    print(f"Local SMTP gateway listening on {host}:{port} → {WEBHOOK_URL}")
    print("Send a forwarded email here (e.g. scripts/demo_send.py). Ctrl-C to stop.")
    try:
        import time

        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        controller.stop()
        print("\nGateway stopped.")


if __name__ == "__main__":
    main()
