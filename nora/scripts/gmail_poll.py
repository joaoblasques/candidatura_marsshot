"""Showcase Gmail-pull entrypoint: poll Nora's demo Gmail inbox and run the pipeline on
each new message — the Gmail-account equivalent of the prod webhook server. Requires the
gmail transport + credentials (NORA_TRANSPORT=gmail, NORA_GMAIL_CREDENTIALS_JSON,
NORA_GMAIL_TENANT_USER). Runs only against the isolated showcase account.

    uv run python -m scripts.gmail_poll            # poll every 15s
    uv run python -m scripts.gmail_poll 30         # poll every 30s
"""

from __future__ import annotations

import sys
import time

from app.config import get_settings
from app.email.gmail_inbound import GmailInboundPoller, build_gmail_read_service
from app.logging import configure_logging, get_logger
from app.main import _run_pipeline, build_default_dependencies

logger = get_logger(__name__)


def main(argv: list[str]) -> int:
    interval = int(argv[0]) if argv else 15
    configure_logging()
    settings = get_settings()
    settings.require_bootable()
    if settings.transport != "gmail":
        print("FAIL: set NORA_TRANSPORT=gmail (+ gmail credentials) for the showcase poller.")
        return 2
    deps = build_default_dependencies(settings)
    service = build_gmail_read_service(settings.gmail_credentials_json, settings.gmail_tenant_user)
    poller = GmailInboundPoller(
        service,
        lambda email: _run_pipeline(deps, settings, email),
        trusted_authserv_id=settings.gmail_trusted_authserv_id,
    )
    logger.info("gmail poller started for %s (every %ds)", settings.gmail_tenant_user, interval)
    while True:
        try:
            n = poller.poll_once()
            if n:
                logger.info("gmail poll: dispatched %d message(s)", n)
        except Exception:  # noqa: BLE001 — a transient API error must not kill the loop
            logger.exception("gmail poll iteration failed")
        time.sleep(interval)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
