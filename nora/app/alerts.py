"""Real-time ops failure alerting.

When the drafting pipeline fails, the affected CEO already gets a failure receipt.
This additionally alerts a *maintainer* address so we learn about failures across
all users in real time — the external uptime monitor only catches "down", and the
weekly metrics readout is lagging. Best-effort and throttled: it must never raise
into the pipeline, and never log email content (only event_id + a salted ceo_hash).
"""

from __future__ import annotations

import time
from collections.abc import Callable

from app.logging import get_logger, redact
from app.metrics import hash_id
from app.receipt import ComposedReply

logger = get_logger(__name__)


class AlertThrottle:
    """Allow at most one alert per ``min_interval_seconds`` (single-instance, in-memory).

    Consistent with the app's in-memory state constraint. The clock is injectable
    for deterministic tests.
    """

    def __init__(self, min_interval_seconds: float, clock: Callable[[], float] = time.monotonic):
        self._min = float(min_interval_seconds)
        self._clock = clock
        self._last: float | None = None

    def allow(self) -> bool:
        now = self._clock()
        if self._last is None or (now - self._last) >= self._min:
            self._last = now
            return True
        return False


def maybe_send_failure_alert(
    outbound, throttle, settings, ceo_id, event_id, ceo_address, sender
) -> bool:
    """Send a redacted ops failure-alert if enabled, distinct from the CEO, and not
    throttled. Returns whether an alert was sent. Never raises.

    ``ceo_id`` is the CEO's ``secret_address_local`` (used only for a salted hash —
    never logged or emailed as-is). ``event_id`` identifies the triggering inbound event.

    Skips when ``alert_email`` is empty or equals ``ceo_address`` (that CEO already
    received the failure receipt — don't double-email the operator for their own
    failures).
    """
    alert_email = (settings.alert_email or "").strip()
    if not alert_email or alert_email == ceo_address:
        return False
    if not throttle.allow():
        return False
    try:
        ceo_hash = hash_id(ceo_id, settings.metrics_salt)
        body = (
            "A Nora pipeline failure occurred — nothing was sent to the recipient.\n\n"
            f"event_id: {event_id}\n"
            f"ceo_hash: {ceo_hash}\n"
        )
        outbound.send_to_ceo(
            alert_email,
            ComposedReply(subject="Nora · pipeline failure", body=body),
            reply_to=None,
            sender=sender,
        )
        return True
    except Exception:  # noqa: BLE001 — alert is best-effort; never break the pipeline
        logger.exception("ops failure-alert send failed for %s", redact({"event_id": event_id}))
        return False
