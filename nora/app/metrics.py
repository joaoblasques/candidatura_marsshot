"""Privacy-safe product-metrics emission (passive signals + implicit proxies).

Emits structured ``event=metric`` lines into the existing app logger, carrying
only non-sensitive, hashed, or aggregate fields — **never** email content,
subject, From, secret address, or live correlation tokens. Metrics themselves
(rates, proxies, retention) are computed offline by ``scripts/metrics_readout.py``
from these raw events: emit-raw, compute-in-readout, so definitions can change
without re-instrumenting (KTD1).

Identity grouping uses a salted, non-reversible hash (KTD2): the readout can count
per-CEO and join revises to drafts without any identity or live token in the log.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone

from app.logging import get_logger

logger = get_logger("app.metrics")

# Defense-in-depth: these keys must never appear raw in a metric event, even if a
# caller passes one. Callers should pass only non-sensitive scalars; this is the
# backstop. Mirrors the intent of app.logging._SENSITIVE_KEYS.
_FORBIDDEN_KEYS = frozenset(
    {
        "body",
        "subject",
        "text",
        "from_address",
        "original_sender",
        "recipients",
        "recipient",  # the third party of a consent-gated action — never in a metric
        "ceo_registry",
        "secret_address_local",
        "reply_to",
        "correlation_token",
        "token",
        "voice_exemplar",
    }
)

_DEFAULT_SALT = "nora-metrics-v1"
_HASH_LEN = 16


def hash_id(value: str, salt: str) -> str:
    """Stable, non-reversible short id for grouping (``ceo_hash`` / ``thread_key``).

    Same value+salt → same id; different salt → different id. Empty salt falls back
    to a fixed default so events are still hashed (never raw identity).
    """
    salted = (salt or _DEFAULT_SALT).encode("utf-8")
    payload = (value or "").strip().lower().encode("utf-8")
    return hashlib.sha256(salted + b":" + payload).hexdigest()[:_HASH_LEN]


_SUBJECT_PREFIX = re.compile(r"^\s*(?:re|fwd|fw)\s*:\s*", re.IGNORECASE)
_NO_SUBJECT = "(no-subject)"


def normalize_subject(subject: str) -> str:
    """Strip leading Re:/Fwd:/Fw: prefixes, lowercase, collapse whitespace.

    Content-free normalization so the same thread re-forwarded produces one key.
    """
    text = subject or ""
    while True:
        stripped = _SUBJECT_PREFIX.sub("", text, count=1)
        if stripped == text:
            break
        text = stripped
    return " ".join(text.lower().split())


def thread_key(subject: str, salt: str) -> str:
    """Stable, non-reversible key for "same thread" detection (U4).

    A best-effort, content-free proxy: the raw subject is never stored, and edited
    subjects / collisions are accepted noise (not load-bearing).
    """
    return hash_id(normalize_subject(subject) or _NO_SUBJECT, salt)


def build_metric_event(
    kind: str, *, ceo_id: str | None = None, salt: str = "", **fields: object
) -> dict[str, object]:
    """Build the redacted metric-event dict. Pure (except the UTC timestamp).

    ``ceo_id`` (raw secret-address local-part or From) is hashed into ``ceo_hash``
    and never stored raw. Forbidden keys in ``fields`` are dropped defensively.
    """
    event: dict[str, object] = {
        "event": "metric",
        "kind": kind,
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    if ceo_id:
        event["ceo_hash"] = hash_id(ceo_id, salt)
    for key, value in fields.items():
        if key in _FORBIDDEN_KEYS:
            continue
        event[key] = value
    return event


def emit(kind: str, *, ceo_id: str | None = None, salt: str = "", **fields: object) -> None:
    """Emit one redacted metric event to the log. Never raises into the caller."""
    try:
        event = build_metric_event(kind, ceo_id=ceo_id, salt=salt, **fields)
        logger.info("metric %s", json.dumps(event, sort_keys=True, default=str))
    except Exception:  # noqa: BLE001 — instrumentation must never break the pipeline
        logger.debug("metric emit failed", exc_info=True)
