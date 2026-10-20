"""Per-context posture: which assurance rung a relationship is handled at.

The unit that sits on a rung is the relationship (the counterparty), not the account
(STRATEGY: Per-Context Posture). Today only R1 is running; a relationship resolved to a
higher rung is declined upstream (its content never reaches the external LLM) until that
rung is built. Empty config => everything R1 => no behavior change."""

from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING

from pydantic import BaseModel

from app.logging import get_logger

if TYPE_CHECKING:
    from app.config import Settings

logger = get_logger(__name__)


class Rung(str, Enum):
    R1 = "R1"
    R2 = "R2"
    R3 = "R3"


class Posture(BaseModel):
    rung: Rung = Rung.R1


def resolve_posture(counterparty: str | None, settings: Settings) -> Posture:
    """Resolve a relationship's posture from the counterparty address. Exact-address
    match wins over domain match; default R1. Unknown rung strings fall back to R1."""
    party = (counterparty or "").strip().lower()
    if not party or not settings.posture_map:
        return Posture(rung=Rung.R1)
    mapping = {k.strip().lower(): v for k, v in settings.posture_map.items()}
    raw = mapping.get(party)
    if raw is None and "@" in party:
        raw = mapping.get(party.split("@", 1)[1])
    if raw is None:
        return Posture(rung=Rung.R1)
    try:
        return Posture(rung=Rung(str(raw).strip().upper()))
    except ValueError:
        logger.warning("posture_map has unknown rung %r — treating as R1", raw)
        return Posture(rung=Rung.R1)
