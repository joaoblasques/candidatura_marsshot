"""Inbound webhook handling (U2).

Order of operations is security-critical: the webhook **HTTP Basic Auth**
credential is verified before any JSON deserialization (KTD4 — Postmark POSTs to
a Basic-Auth URL; this replaces the v1.0 HMAC body-signature). Note the
trade-off: Basic Auth authenticates the *caller*, not the body's integrity, which
is acceptable for a single trusted provider over HTTPS. After auth the payload is
parsed, adapted to the normalized ``InboundEmail`` by a per-provider ``adapt``
callable (e.g. ``app/email/postmark_inbound.py``), then replay-, auth-, and
rate-checked. The gauntlet stays provider-agnostic by taking ``adapt`` as a
parameter (KTD2).
"""

from __future__ import annotations

import base64
import binascii
import hmac
import json
from collections.abc import Callable
from dataclasses import dataclass

from app.auth.sender import authenticate
from app.config import Settings
from app.logging import get_logger, redact
from app.metrics import emit, thread_key
from app.models import InboundEmail
from app.security import RateLimiter, ReplayGuard

logger = get_logger(__name__)


@dataclass
class InboundOutcome:
    """Result of running the inbound gauntlet. ``email`` is set only when authenticated."""

    status_code: int
    reason: str
    email: InboundEmail | None = None


def verify_basic_auth(authorization_header: str | None, expected: str) -> bool:
    """Constant-time check of an HTTP Basic ``Authorization`` header (KTD4).

    ``expected`` is the configured ``user:password`` credential. Returns False for
    a missing/malformed header or an empty expected credential.
    """
    if not authorization_header or not expected:
        return False
    scheme, _, encoded = authorization_header.partition(" ")
    if scheme.lower() != "basic" or not encoded:
        return False
    try:
        decoded = base64.b64decode(encoded.strip(), validate=True).decode("utf-8")
    except (binascii.Error, ValueError):
        return False
    return hmac.compare_digest(decoded, expected)


def split_secret_address(address: str) -> tuple[str, str | None]:
    """Split ``nora-abc123+corrid@domain`` into (``nora-abc123``, ``corrid``).

    The plus-tag carries the revise-loop correlation token (KTD6); a bare
    address returns ``None`` for the token. Inverse of ``build_secret_address``.
    """
    local = address.split("@", 1)[0].strip().lower()
    if "+" in local:
        base, token = local.split("+", 1)
        return base, (token or None)
    return local, None


def build_secret_address(local: str, token: str, domain: str) -> str:
    """Build the plus-addressed reply-to ``local+token@domain`` (KTD6).

    Inverse of ``split_secret_address`` — the encode/decode pair lives together
    so the correlation-address format has a single definition.
    """
    return f"{local.strip().lower()}+{token}@{domain}"


def parse_body(raw_body: bytes) -> dict:
    """Deserialize the verified raw body. Raises ValueError on malformed JSON."""
    try:
        data = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise ValueError("malformed webhook payload") from exc
    if not isinstance(data, dict):
        raise ValueError("webhook payload must be a JSON object")
    return data


def process_inbound(
    raw_body: bytes,
    authorization_header: str | None,
    client_key: str,
    settings: Settings,
    replay_guard: ReplayGuard,
    rate_limiter: RateLimiter,
    adapt: Callable[[dict], InboundEmail],
    user_store: object = None,
) -> InboundOutcome:
    """Run the full inbound gauntlet: auth → rate-limit → parse → adapt → replay → auth.

    ``adapt`` maps the provider's parsed JSON into the normalized ``InboundEmail``.
    Returns an outcome whose ``email`` is populated only for authenticated
    requests. Unauthenticated senders are dropped with 200 so the provider does
    not retry; webhook-credential failures return 401; rate-limit returns 429.
    """
    salt = settings.metrics_salt

    # 1. Verify the webhook Basic-Auth credential before any deserialization.
    if not verify_basic_auth(authorization_header, settings.webhook_basic_auth):
        emit("inbound", salt=salt, outcome="bad_credential")
        return InboundOutcome(401, "invalid webhook credential")

    # 2. Coarse per-source rate limit (DoS guard) before doing further work.
    #    ``client_key`` is the real client IP (CF-Connecting-IP behind the tunnel),
    #    so this is a genuine per-source flood guard; the meaningful per-CEO limit
    #    is applied after auth below.
    if not rate_limiter.allow(f"ip:{client_key}"):
        emit("inbound", salt=salt, outcome="rate_limited", scope="ip")
        return InboundOutcome(429, "rate limited")

    # 3. Parse the body and adapt it to the normalized shape.
    try:
        payload = parse_body(raw_body)
    except ValueError:
        emit("inbound", salt=salt, outcome="malformed")
        return InboundOutcome(400, "malformed payload")
    try:
        email = adapt(payload)
    except (KeyError, TypeError, ValueError, AttributeError):
        # A hostile/malformed provider payload must degrade to 400, never a 500
        # (which the provider would retry). AttributeError guards against
        # non-dict elements where the adapter expects dict-shaped items.
        emit("inbound", salt=salt, outcome="unmappable")
        return InboundOutcome(400, "unmappable provider payload")

    # 4. Replay protection by provider message/event id.
    if not email.event_id or replay_guard.is_replay(email.event_id):
        emit("inbound", ceo_id=email.secret_address_local, salt=salt, outcome="replay")
        return InboundOutcome(200, "duplicate or missing event id")

    # 5. Authenticate the sender (From-match against the registry + DKIM). The registry is the
    # static env merged with any dynamically-provisioned (self-serve) users. The signup
    # local-part is a special case: the sender isn't registered yet (we're about to create the
    # entry), so it's accepted on DKIM alone — ownership proof — and the pipeline enforces the
    # invite allowlist before provisioning.
    from app.users import effective_registry

    merged = effective_registry(settings.ceo_registry, user_store)
    is_signup = bool(settings.signup_local) and email.secret_address_local == settings.signup_local
    if is_signup:
        authed = email.dkim_verified
    else:
        authed = authenticate(email, merged)
    if not authed:
        # Drop with 200 so the provider does not retry an unauthorized message.
        # Log (redacted) so spoof/abuse attempts are visible without amplification.
        logger.warning(
            "inbound rejected: unauthorized sender %s",
            redact(
                {
                    "event_id": email.event_id,
                    "secret_address_local": email.secret_address_local,
                    "dkim_verified": email.dkim_verified,
                }
            ),
        )
        emit(
            "inbound",
            ceo_id=email.secret_address_local,
            salt=salt,
            outcome="unauthorized",
            dkim_verified=email.dkim_verified,
        )
        return InboundOutcome(200, "unauthorized sender")

    # 6. Per-CEO rate limit, isolated from other CEOs (keyed by secret address).
    if not rate_limiter.allow(f"ceo:{email.secret_address_local}"):
        emit("inbound", ceo_id=email.secret_address_local, salt=salt, outcome="rate_limited", scope="ceo")
        return InboundOutcome(429, "rate limited")

    emit(
        "inbound",
        ceo_id=email.secret_address_local,
        salt=salt,
        outcome="accepted",
        thread_key=thread_key(email.subject, salt),
    )
    return InboundOutcome(202, "accepted", email=email)
