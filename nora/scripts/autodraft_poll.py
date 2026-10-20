"""Ambient auto-draft poller (connected mailbox). Polls the inbox and saves a reply DRAFT
— **never sends** — for genuine person-to-person mail into the owner's own Drafts, threaded to
the original. Default-off: requires NORA_AUTODRAFT=true + NORA_TRANSPORT=gmail + gmail credentials.
Operator-run, on the connected mailbox only.

    uv run python -m scripts.autodraft_poll          # poll every 30s
    uv run python -m scripts.autodraft_poll 60       # poll every 60s

Idempotency is by label (NORA_AUTODRAFT_LABEL, default "Nora/seen" — a PROCESSED marker); the exec's UNREAD state
is never touched. One Gmail service (gmail.modify scope) covers list/get/labels/drafts.create.
"""

from __future__ import annotations

import sys
import time

from app.autodraft import autodraft_once
from app.config import get_settings
from app.email.gmail_inbound import build_gmail_read_service
from app.logging import configure_logging, get_logger
from app.main import build_default_dependencies
from app.tenant import GmailDraftTenantClient

# Under the "app.*" namespace so configure_logging()'s INFO level applies — a bare
# "scripts.*" logger inherits root WARNING and its startup/poll info lines vanish.
logger = get_logger("app.autodraft_poll")


def _get_or_create_label(service, user_id: str, name: str) -> str:
    labels = service.users().labels().list(userId=user_id).execute().get("labels", [])
    for lbl in labels:
        if lbl.get("name") == name:
            return str(lbl["id"])
    created = (
        service.users().labels()
        .create(userId=user_id, body={
            "name": name, "labelListVisibility": "labelShow", "messageListVisibility": "show",
        })
        .execute()
    )
    return str(created["id"])


def main(argv: list[str]) -> int:
    interval = int(argv[0]) if argv else 30
    configure_logging()
    settings = get_settings()
    settings.require_bootable()
    if not settings.autodraft:
        print("FAIL: set NORA_AUTODRAFT=true to run the ambient auto-draft poller.")
        return 2
    if settings.transport != "gmail":
        print("FAIL: set NORA_TRANSPORT=gmail (+ gmail credentials) for auto-draft.")
        return 2
    deps = build_default_dependencies(settings)
    user = settings.gmail_tenant_user
    # Auth: OAuth for a personal @gmail.com (no domain-wide delegation possible), else the
    # Workspace service-account impersonation. OAuth calls resolve the user as "me".
    if settings.gmail_auth == "oauth":
        from app.email.gmail_oauth import build_gmail_oauth_service
        service = build_gmail_oauth_service(settings.gmail_oauth_token)
        mailbox = "me"
    else:
        service = build_gmail_read_service(settings.gmail_credentials_json, user)
        mailbox = user
    drafts = GmailDraftTenantClient(mailbox, service=service)
    from app.mailbox.provider import GmailMailboxProvider
    from app.inbox_rules import FileInboxRuleStore
    from app.draft_feedback import FileDraftFeedbackStore
    provider = GmailMailboxProvider(service, user_id=mailbox)  # safe inbox actions (gmail.modify)
    rules = FileInboxRuleStore(settings.inbox_rules_path)
    feedback = FileDraftFeedbackStore(settings.draft_feedback_path)
    filed_label_id = (
        _get_or_create_label(service, mailbox, settings.filed_label)
        if settings.inbox_actions else None
    )
    label_id = _get_or_create_label(service, mailbox, settings.autodraft_label)
    label_q = settings.autodraft_label.replace(" ", "-")  # Gmail search uses the label name
    owner_local = user or mailbox  # stable per-exec key for voice/memory recall
    logger.info("autodraft poller started for %s (every %ds; inbox_actions=%s)",
                user or mailbox, interval, settings.inbox_actions)
    while True:
        try:
            n = autodraft_once(
                service, user_id=mailbox, label_id=label_id,
                query=f"is:unread -label:{label_q}",
                draft_reply=lambda s, sub, b: deps.pipeline.autodraft_reply(owner_local, s, sub, b),
                save_draft=drafts.save_draft,
                provider=provider, alias=settings.command_alias,
                inbox_actions=settings.inbox_actions,
                rules=rules, owner=owner_local, filed_label_id=filed_label_id,
                feedback=feedback, draft_learning=settings.draft_learning,
            )
            if n:
                logger.info("autodraft: drafted %d repl(y/ies)", n)
        except Exception:  # noqa: BLE001 — a transient API error must not kill the loop
            logger.exception("autodraft poll iteration failed")
        time.sleep(interval)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
