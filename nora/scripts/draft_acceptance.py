"""Draft-acceptance readout — the pilot north-star KPI.

Prints how many auto-drafts were created, sent, and deleted for an owner, and the acceptance rate
(sent among decided drafts). Reads the draft-feedback JSON store the poller maintains — no network,
no DB. Run on the host against the store file:

    uv run python -m scripts.draft_acceptance .nora-draft-feedback.json <owner-email>

Acceptance rate is the retention proxy that matters for a pilot (Fyxer's whole pitch is "less
editing before send"). With few drafts it's directional, not statistical.
"""

from __future__ import annotations

import argparse

from app.draft_feedback import FileDraftFeedbackStore


def format_readout(stats: dict[str, object], owner: str) -> str:
    created = int(stats["created"])  # type: ignore[arg-type]
    sent = int(stats["sent"])  # type: ignore[arg-type]
    deleted = int(stats["deleted"])  # type: ignore[arg-type]
    pending = created - sent - deleted
    rate = stats["acceptance_rate"]
    rate_str = "n/a (no decided drafts yet)" if rate is None else f"{float(rate) * 100:.0f}%"
    return (
        f"Draft acceptance — {owner}\n"
        f"  created:   {created}\n"
        f"  sent:      {sent}\n"
        f"  deleted:   {deleted}\n"
        f"  pending:   {pending}\n"
        f"  acceptance rate (sent / decided): {rate_str}"
    )


def main() -> None:
    ap = argparse.ArgumentParser(description="Draft-acceptance KPI readout")
    ap.add_argument("store", help="path to the draft-feedback JSON store")
    ap.add_argument("owner", help="owner email (the connected mailbox)")
    args = ap.parse_args()
    stats = FileDraftFeedbackStore(args.store).stats(args.owner)
    print(format_readout(stats, args.owner))


if __name__ == "__main__":
    main()
