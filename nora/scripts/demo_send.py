"""Send demo 'forwarded thread' emails into the local SMTP gateway (DEV ONLY).

Simulates a CEO forwarding a thread to Nora. Each message is sent over real SMTP
to the local gateway, which relays it to the app's webhook; Nora's reply lands in
Mailpit (http://127.0.0.1:8025). See deploy/LOCAL_DEMO.md.

Env:
    NORA_SMTP_GATEWAY_HOST/PORT   default 127.0.0.1:2525
    NORA_DEMO_HASH                the inbound hash / registry key (default abc123hash)
    NORA_DEMO_CEO                 the registered CEO From (default ceo@example.com)
"""

from __future__ import annotations

import os
import smtplib
import sys
from email.message import EmailMessage

HOST = os.environ.get("NORA_SMTP_GATEWAY_HOST", "127.0.0.1")
PORT = int(os.environ.get("NORA_SMTP_GATEWAY_PORT", "2525"))
HASH = os.environ.get("NORA_DEMO_HASH", "abc123hash")
CEO = os.environ.get("NORA_DEMO_CEO", "ceo@example.com")

_THREAD = (
    "---------- Forwarded message ----------\n"
    "From: Sarah Chen <sarah@acme.com>\n"
    "To: CEO <{ceo}>\n"
    "Subject: Partnership proposal\n\n"
    "Hi — we'd love to explore a partnership with you. Could you share a time "
    "next week to talk through scope and pricing?\n"
).format(ceo=CEO)

# (code, subject, optional +tag for revise correlation)
SCENARIOS = {
    "wiyow": ("WIYOW", "Fwd: Partnership proposal", ""),
    "summarize": ("S", "Fwd: Partnership proposal", ""),
    "english": ("Draft a warm reply saying yes and proposing Tuesday.", "Fwd: Partnership proposal", ""),
}


def _send(code: str, subject: str, tag: str) -> None:
    recipient = f"{HASH}+{tag}@inbound.postmarkapp.com" if tag else f"{HASH}@inbound.postmarkapp.com"
    msg = EmailMessage()
    msg["From"] = CEO
    msg["To"] = recipient
    msg["Subject"] = subject
    msg.set_content(f"{code}\n\n{_THREAD}")
    with smtplib.SMTP(HOST, PORT, timeout=15) as smtp:
        smtp.send_message(msg)
    print(f"sent [{code[:20]}…] → {recipient}")


def main() -> None:
    which = sys.argv[1] if len(sys.argv) > 1 else "wiyow"
    if which == "all":
        for code, subject, tag in SCENARIOS.values():
            _send(code, subject, tag)
    elif which in SCENARIOS:
        _send(*SCENARIOS[which])
    else:
        print(f"unknown scenario '{which}'. Options: {', '.join(SCENARIOS)}, all")
        sys.exit(1)
    print("Check Mailpit at http://127.0.0.1:8025 for Nora's reply.")


if __name__ == "__main__":
    main()
