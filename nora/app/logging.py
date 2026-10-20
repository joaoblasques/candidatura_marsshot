"""Logging helpers with redaction of sensitive email fields (KTD10).

Email body, subject, and sender addresses are sensitive: they must never appear
in logs at INFO or above, and must be stripped from exception context. These
helpers centralize that policy so call sites can log safely.
"""

from __future__ import annotations

import logging

_SENSITIVE_KEYS = {
    "body",
    "subject",
    "from_address",
    "original_sender",
    "recipients",
    "recipient",  # the third party a consent-gated action sends to — an address, never log it
    "text",
    # The registry maps secret local-parts to real CEO addresses, and the secret
    # address / reply-to carry the live correlation token — never log these.
    "ceo_registry",
    "secret_address_local",
    "reply_to",
    "voice_exemplar",  # the CEO's own thread prose — content, never log it
}


def configure_logging(level: int = logging.INFO) -> None:
    """Enable INFO logging for the ``app.*`` loggers.

    Configures the ``app`` parent logger directly (handler + level) rather than
    the root logger, so it survives uvicorn installing its own root config — a
    bare ``basicConfig`` can be overridden once uvicorn configures logging.
    Idempotent via the handler check: safe to call on every app construction.
    """
    app_logger = logging.getLogger("app")
    app_logger.setLevel(level)
    if not app_logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
        )
        app_logger.addHandler(handler)
    app_logger.propagate = False


def redact(data: dict[str, object]) -> dict[str, object]:
    """Return a copy of ``data`` with sensitive fields replaced by a marker.

    Used before logging any structure derived from an inbound email.
    """
    redacted: dict[str, object] = {}
    for key, value in data.items():
        if key in _SENSITIVE_KEYS and value not in (None, "", [], {}):
            redacted[key] = "<redacted>"
        else:
            redacted[key] = value
    return redacted


def get_logger(name: str) -> logging.Logger:
    """Return a module logger. Callers must pass already-redacted structures."""
    return logging.getLogger(name)
