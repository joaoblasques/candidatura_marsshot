"""Cross-thread relationship memory — Layer A (spike's smallest-safe slice).

Per-CEO, **metadata-only** breadcrumbs: how many times the CEO has had Nora draft to a
counterparty, and when last. NO message content / subjects are stored, so there is nothing
sensitive to re-leak and the keep-local (KL/R3) guarantee is structurally safe — the
pipeline only ever records/recalls on the R1 path. Default backend is Null (off).

See docs/brainstorms/2026-06-17-cross-thread-memory-spike.md.
"""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel

from app.state.json_file_map import atomic_write_json_map, read_json_map

_MAX_PER_CEO = 500  # bound growth: keep the most-recently-seen counterparties per CEO
_MAX_NOTES = 10     # Layer C: bound CEO-curated facts kept per counterparty (newest wins)
_MAX_DERIVED = 8    # Layer C v2: bound auto-derived facts kept per counterparty (newest wins)
_MAX_FACT_LEN = 160  # a derived fact is a terse clause, never a paragraph


class RelationshipMemory(BaseModel):
    counterparty: str
    thread_count: int = 0
    last_seen: str = ""  # ISO date (YYYY-MM-DD); no time/content
    # Layer C: durable facts the CEO explicitly asked Nora to remember about this counterparty.
    # Recalled into the draft context on the R1 path only.
    notes: list[str] = []
    # Layer C v2: facts Nora auto-derived from thread content (provenance-tagged, kept SEPARATE
    # from curated `notes` so recall can frame them as observed/unconfirmed). R1-only; only
    # populated when NORA_RELATIONSHIP_AUTOLEARN is on.
    derived: list[str] = []


class MemoryStore(Protocol):
    def recall(self, ceo_local: str, counterparty: str) -> RelationshipMemory | None: ...
    def record(self, ceo_local: str, counterparty: str, when: str) -> None: ...
    def remember(self, ceo_local: str, counterparty: str, fact: str) -> None: ...
    def learn(self, ceo_local: str, counterparty: str, facts: list[str]) -> None: ...
    def forget_notes(self, ceo_local: str, counterparty: str) -> None: ...


class NullMemoryStore:
    """Default: memory off — recall finds nothing; record/remember/learn/forget are no-ops."""

    def recall(self, ceo_local: str, counterparty: str) -> RelationshipMemory | None:
        return None

    def record(self, ceo_local: str, counterparty: str, when: str) -> None:
        return None

    def remember(self, ceo_local: str, counterparty: str, fact: str) -> None:
        return None

    def learn(self, ceo_local: str, counterparty: str, facts: list[str]) -> None:
        return None

    def forget_notes(self, ceo_local: str, counterparty: str) -> None:
        return None


def _key(ceo_local: str, counterparty: str) -> str:
    return f"{ceo_local.strip().lower()}|{counterparty.strip().lower()}"


class FileMemoryStore:
    """Per-CEO breadcrumbs in a JSON map keyed ``ceo_local|counterparty`` (atomic,
    corrupt-safe, same I/O as the other file stores). Bounded per CEO."""

    def __init__(self, path: str) -> None:
        self._path = path

    def recall(self, ceo_local: str, counterparty: str) -> RelationshipMemory | None:
        if not ceo_local or not counterparty:
            return None
        rec = read_json_map(self._path).get(_key(ceo_local, counterparty))
        if not isinstance(rec, dict):
            return None
        notes = rec.get("notes")
        derived = rec.get("derived")
        return RelationshipMemory(
            counterparty=counterparty,
            thread_count=int(rec.get("thread_count", 0)),
            last_seen=str(rec.get("last_seen", "")),
            notes=[str(n) for n in notes] if isinstance(notes, list) else [],
            derived=[str(d) for d in derived] if isinstance(derived, list) else [],
        )

    def record(self, ceo_local: str, counterparty: str, when: str) -> None:
        if not ceo_local or not counterparty:
            return
        data = read_json_map(self._path)
        key = _key(ceo_local, counterparty)
        prior = data.get(key) if isinstance(data.get(key), dict) else {}
        data[key] = {
            "thread_count": int(prior.get("thread_count", 0)) + 1,
            "last_seen": when,
            "notes": prior.get("notes", []),  # preserve Layer C facts across breadcrumb updates
            "derived": prior.get("derived", []),  # preserve auto-derived facts too
        }
        _bound_per_ceo(data, ceo_local)
        atomic_write_json_map(self._path, data)

    def remember(self, ceo_local: str, counterparty: str, fact: str) -> None:
        """Append a CEO-curated fact about this counterparty (dedup, newest-capped)."""
        fact = (fact or "").strip()
        if not ceo_local or not counterparty or not fact:
            return
        data = read_json_map(self._path)
        key = _key(ceo_local, counterparty)
        prior = data.get(key) if isinstance(data.get(key), dict) else {}
        prior_notes = prior.get("notes", [])
        notes = [str(n) for n in prior_notes] if isinstance(prior_notes, list) else []
        if fact not in notes:
            notes.append(fact)
        data[key] = {
            "thread_count": int(prior.get("thread_count", 0)),
            "last_seen": str(prior.get("last_seen", "")),
            "notes": notes[-_MAX_NOTES:],  # keep the newest facts only
            "derived": prior.get("derived", []),  # preserve auto-derived facts
        }
        _bound_per_ceo(data, ceo_local)
        atomic_write_json_map(self._path, data)

    def learn(self, ceo_local: str, counterparty: str, facts: list[str]) -> None:
        """Append auto-derived facts (Layer C v2): trim each, case-insensitive dedup against
        both `derived` and the curated `notes` (never re-learn a curated fact), newest-capped."""
        if not ceo_local or not counterparty or not facts:
            return
        data = read_json_map(self._path)
        key = _key(ceo_local, counterparty)
        prior = data.get(key) if isinstance(data.get(key), dict) else {}
        prior_derived = prior.get("derived", [])
        derived = [str(d) for d in prior_derived] if isinstance(prior_derived, list) else []
        prior_notes = prior.get("notes", [])
        notes = [str(n) for n in prior_notes] if isinstance(prior_notes, list) else []
        seen = {f.lower() for f in derived} | {n.lower() for n in notes}
        for raw in facts:
            fact = (str(raw) or "").strip()[:_MAX_FACT_LEN]
            if fact and fact.lower() not in seen:
                derived.append(fact)
                seen.add(fact.lower())
        data[key] = {
            "thread_count": int(prior.get("thread_count", 0)),
            "last_seen": str(prior.get("last_seen", "")),
            "notes": notes[-_MAX_NOTES:],
            "derived": derived[-_MAX_DERIVED:],  # keep the newest derived facts only
        }
        _bound_per_ceo(data, ceo_local)
        atomic_write_json_map(self._path, data)

    def forget_notes(self, ceo_local: str, counterparty: str) -> None:
        """Clear all facts for this counterparty — curated AND auto-derived (keeps the
        breadcrumb). One forget verb wipes everything Nora believes about the contact."""
        if not ceo_local or not counterparty:
            return
        data = read_json_map(self._path)
        key = _key(ceo_local, counterparty)
        rec = data.get(key)
        if isinstance(rec, dict) and (rec.get("notes") or rec.get("derived")):
            rec["notes"] = []
            rec["derived"] = []
            data[key] = rec
            atomic_write_json_map(self._path, data)


def _bound_per_ceo(data: dict[str, dict], ceo_local: str) -> None:
    """Keep at most _MAX_PER_CEO counterparties per CEO (drop the oldest last_seen)."""
    prefix = f"{ceo_local.strip().lower()}|"
    mine = [(k, v) for k, v in data.items() if k.startswith(prefix)]
    if len(mine) <= _MAX_PER_CEO:
        return
    mine.sort(key=lambda kv: str(kv[1].get("last_seen", "")))  # oldest first
    for k, _ in mine[: len(mine) - _MAX_PER_CEO]:
        del data[k]
