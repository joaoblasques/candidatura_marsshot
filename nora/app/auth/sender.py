"""Sender authentication (KTD4).

v1.0 has no mailbox OAuth. A request is trusted only when it arrived at a known
per-CEO secret address AND the DKIM-verified From matches that CEO's registered
address. The DKIM result is supplied by the email provider (which performs the
check); we reject anything not DKIM-verified or whose From does not align.
"""

from __future__ import annotations

from app.models import InboundEmail


def allowed_senders(registered: str | list[str] | None) -> list[str]:
    """Normalize a registry value (single address or list) to a lowercased list."""
    if registered is None:
        return []
    values = [registered] if isinstance(registered, str) else list(registered)
    return [v.strip().lower() for v in values if v and v.strip()]


def authenticate(email: InboundEmail, registry: dict[str, str | list[str]]) -> bool:
    """Return True only for a registered, DKIM-aligned CEO forward.

    A registry entry may list several allowed From addresses for one person; the
    sender must match one of them.
    """
    if not email.dkim_verified:
        return False
    allowed = allowed_senders(registry.get(email.secret_address_local))
    if not allowed:
        return False
    return email.from_address.strip().lower() in allowed
