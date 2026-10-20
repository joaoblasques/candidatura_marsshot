"""Volume/triage (TR) — split a bundled forward into per-thread items for a ranked digest.
Pure + content-only (no network, no state). The digest itself is produced by the drafting
pipeline via Intent.TRIAGE."""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.codes import parse_code_token

# Per-item actions a triage follow-up may request (read/draft only — the consent-gated actions
# SM/BM/SH keep their own proposal flow; forward that thread normally to use them).
SUPPORTED_FOLLOWUP_ACTIONS = frozenset({"RE", "WIYOW", "SC", "S", "AC", "AR", "PR"})

_BRACKET = re.compile(r"\[(.*?)\]")


def relationship_hint(thread_count: int, last_seen: str, notes: list[str]) -> str:
    """A terse familiarity note for a triaged thread's sender, from the Layer A/C breadcrumb
    (Triage v2). ``""`` is never returned here — callers pass ``""`` themselves when there is no
    sender or memory is off; this only formats a known recall."""
    if thread_count > 0:
        plural = "s" if thread_count != 1 else ""
        last = f", last {last_seen}" if last_seen else ""
        parts = [f"recurring sender ({thread_count} prior exchange{plural}{last})"]
    else:
        parts = ["first-time sender"]
    if notes:
        parts.append("you noted: " + "; ".join(notes))
    return "; ".join(parts)


@dataclass(frozen=True)
class TriageFollowupItem:
    """One '<n> <ACTION> [notes]' reference parsed from a reply to a triage digest."""

    index: int            # 1-based item number as the CEO wrote it
    action_code: str      # canonical action head (e.g. "RE"), uppercased
    notes: str | None     # bracketed free-text notes for this item, if any
    raw_code: str         # the code token as written (echoed to drafting for refinements)
    in_range: bool        # 1 <= index <= n_items
    supported: bool       # action_code is a per-item-supported action


def parse_triage_followup(body: str, n_items: int) -> list[TriageFollowupItem]:
    """Parse a reply to a triage digest into per-item action references.

    Accepts lines like ``2 RE``, ``RE 2``, ``2 RE [keep it warm]``, ``4 NB-S.Short`` — one item
    per line. Finds the integer (item number) and the first code token (via the NB grammar, so
    refinements carry through). Junk lines and lines lacking both a number and a code are ignored;
    a given item number is taken once (first wins); index is range-checked and the action is
    checked against the supported set."""
    out: list[TriageFollowupItem] = []
    seen: set[int] = set()
    for raw in (body or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        notes_m = _BRACKET.search(line)
        notes = notes_m.group(1).strip() if notes_m else None
        scan = _BRACKET.sub(" ", line)
        index: int | None = None
        action: str | None = None
        raw_code = ""
        for tok in scan.split():
            if tok.isdigit():
                if index is None:
                    index = int(tok)
                continue
            cp = parse_code_token(tok)
            if cp.action and action is None:
                action = cp.action
                raw_code = cp.raw
        if index is None or action is None or index in seen:
            continue
        seen.add(index)
        out.append(
            TriageFollowupItem(
                index=index,
                action_code=action,
                notes=notes,
                raw_code=raw_code,
                in_range=1 <= index <= n_items,
                supported=action in SUPPORTED_FOLLOWUP_ACTIONS,
            )
        )
    return out

# Standard "this is a forwarded message" separators across the common clients.
_FORWARD_MARKERS = re.compile(
    r"^[ \t]*(?:"
    r"-{2,}[ \t]*Forwarded message[ \t]*-{2,}"   # Gmail
    r"|Begin forwarded message:"                  # Apple Mail
    r"|-{2,}[ \t]*Original Message[ \t]*-{2,}"     # Outlook
    r")[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)


def split_threads(body: str, *, max_items: int = 20) -> list[str]:
    """Split a forwarded body into thread items. With markers: each segment AFTER a marker is
    an item (the lead segment before the first marker — the CEO's note — is dropped). Without
    markers: the whole body is one item. Bounded at ``max_items``."""
    text = body or ""
    if _FORWARD_MARKERS.search(text):
        segments = _FORWARD_MARKERS.split(text)[1:]  # drop pre-first-marker lead note
        items = [s.strip() for s in segments if s.strip()]
    else:
        items = [text.strip()] if text.strip() else []
    return items[:max_items]
