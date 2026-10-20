"""Consent-gated follow-up loop: remind the principal about a still-unapproved proposal, then
let it lapse. Bounded single-pass; recurrence is a system cron. Reminders are self-sends to the
CEO only — never the third party — and AG is still required to act. Pure scanner + injectable
runner; the CLI wires the real CEO-send."""

from __future__ import annotations

import datetime as _dt
import sys
import time
from typing import Callable

from app.logging import configure_logging, get_logger
from app.receipt import ComposedReply
from app.state.store import PendingActionRecord

logger = get_logger(__name__)


def due_for_reminder(
    records: list[PendingActionRecord], now: float, *,
    remind_after_seconds: float, max_reminders: int,
) -> list[PendingActionRecord]:
    out = []
    for r in records:
        if r.status != "pending":
            continue
        if (now - r.created_at) < remind_after_seconds:
            continue
        if r.reminders_sent >= max_reminders:
            continue
        if r.last_reminded_at is not None and (now - r.last_reminded_at) < remind_after_seconds:
            continue
        out.append(r)
    return out


def run_followups(
    store, send_reminder: Callable[[str, PendingActionRecord], None],
    resolve_ceo: Callable[[str], str | None], now: float, *,
    remind_after_seconds: float, max_reminders: int,
) -> int:
    due = due_for_reminder(
        store.all_pending(), now,
        remind_after_seconds=remind_after_seconds, max_reminders=max_reminders,
    )
    sent = 0
    for rec in due:
        ceo = resolve_ceo(rec.owner_secret_local)
        if not ceo:
            continue  # can't resolve the principal -> skip (never guess a recipient)
        send_reminder(ceo, rec)
        rec.reminders_sent += 1
        rec.last_reminded_at = now
        store.put(rec)
        sent += 1
    return sent


def reminder_reply(record: PendingActionRecord, *, lapse_at: float) -> ComposedReply:
    proposed = _dt.datetime.fromtimestamp(record.created_at, _dt.UTC).date().isoformat()
    lapses = _dt.datetime.fromtimestamp(lapse_at, _dt.UTC).date().isoformat()
    body = (
        f'You asked me to propose "{record.subject}" to {record.recipient} on {proposed}, '
        f"but it hasn't been approved yet.\n\n"
        f"Reply AG to go ahead, or just ignore this — the proposal lapses on {lapses}.\n\n"
        f"(I won't send or book anything without your AG.)"
    )
    return ComposedReply(subject=f"Reminder: awaiting your approval — {record.subject}", body=body)


def main(argv: list[str]) -> int:
    from app.config import get_settings
    from app.main import build_default_dependencies

    configure_logging()
    settings = get_settings()
    settings.require_bootable()
    deps = build_default_dependencies(settings)
    store = deps.pipeline.actions

    def resolve_ceo(local: str) -> str | None:
        reg = settings.ceo_registry.get(local)
        if isinstance(reg, list):
            return reg[0] if reg else None
        return reg

    def send_reminder(ceo: str, rec: PendingActionRecord) -> None:
        lapse_at = rec.created_at + settings.pending_action_ttl_seconds
        deps.outbound.send_to_ceo(ceo, reminder_reply(rec, lapse_at=lapse_at), reply_to=None, sender=None)

    n = run_followups(
        store, send_reminder, resolve_ceo, time.time(),
        remind_after_seconds=settings.followup_remind_after_seconds,
        max_reminders=settings.followup_max_reminders,
    )
    logger.info("followups: reminded %d pending proposal(s)", n)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
