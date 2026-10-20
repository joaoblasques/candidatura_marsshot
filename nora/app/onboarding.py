"""R1 self-serve onboarding: eligibility, provisioning, and the signer-facing replies. Pure +
content-only; the pipeline calls these on the signup local-part. DKIM (ownership proof) is gated
upstream in the inbound auth path; here we enforce the invite allowlist + dedup."""

from __future__ import annotations

import secrets

from app.logging import get_logger
from app.receipt import ComposedReply
from app.users import UserStore

logger = get_logger(__name__)


def signup_eligible(from_addr: str, allowlist: list[str]) -> bool:
    """True iff the sender's From is allowlisted — by exact address (`user@domain`), `@domain`,
    or bare `domain`. An empty allowlist admits no one (default-off)."""
    fa = (from_addr or "").strip().lower()
    if not fa or "@" not in fa:
        return False
    domain = fa.split("@", 1)[1]
    for entry in allowlist or []:
        e = entry.strip().lower()
        if not e:
            continue
        if "@" in e and not e.startswith("@") and fa == e:
            return True
        if e.startswith("@") and domain == e[1:]:
            return True
        if "@" not in e and domain == e:
            return True
    return False


def new_secret_local(prefix: str = "nora") -> str:
    """A unique, address-safe secret local-part (e.g. 'nora-3f9a2c7b1d')."""
    return f"{prefix}-{secrets.token_hex(5)}"


def provision_signup(user_store: UserStore, from_addr: str) -> tuple[str, bool]:
    """Return (secret_local, is_new). Dedups by sender; generates a collision-free token otherwise."""
    existing = user_store.find_by_sender(from_addr)
    if existing:
        return existing, False
    taken = set(user_store.all())
    local = new_secret_local()
    while local in taken:
        local = new_secret_local()
    user_store.add_user(local, from_addr)
    return local, True


def welcome_reply(secret_local: str, domain: str) -> ComposedReply:
    address = f"{secret_local}@{domain}"
    body = (
        f"Welcome to Nora. Your private address is:\n\n    {address}\n\n"
        "Forward any email thread there with a short code on the first line — e.g. WIYOW to draft "
        "a reply, or S to summarize — and I'll send a copy-ready result back to you. You always "
        "send; I never send as you. For the full list of codes, email HELP to the address above "
        "(not a reply to this message)."
    )
    return ComposedReply(subject="Your Nora address is ready", body=body)


def access_request_reply(from_addr: str | None = None) -> ComposedReply:
    # Record the request so "we've noted your request" is actually true (the operator can grep the
    # log to follow up). We log only the requester address (already DKIM-verified inbound), nothing
    # sensitive.
    logger.info("signup access requested (off-allowlist): %s", (from_addr or "unknown").strip())
    body = (
        "Thanks for your interest in Nora. Access is invite-only right now, so I couldn't set you "
        "up automatically. We've noted your request — someone will be in touch."
    )
    return ComposedReply(subject="Nora — access request received", body=body)
