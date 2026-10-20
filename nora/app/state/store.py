"""Revise-loop state, correlation, and retention (U7).

A draft reply gets a short-lived record keyed by an opaque correlation token,
which is embedded in the plus-addressed reply-to (KTD6) so the CEO's revise
reply routes back correlated even when clients mangle Message-ID/subject. The
store is behind a small interface so a single-instance in-memory default can be
swapped for a shared/persistent backend in deployment (U8). Records carry a
short TTL and are purged so thread content is not retained beyond the loop
(KTD5).
"""

from __future__ import annotations

import secrets
import time
from typing import Protocol

from pydantic import BaseModel, Field

from app.logging import get_logger
from app.models import ParsedThread, ResultKind
from app.state.json_file_map import atomic_write_json_map, read_json_map

logger = get_logger(__name__)


def new_correlation_token() -> str:
    """An opaque, address-safe correlation token (128 bits; hex stays valid in a
    plus-addressed local-part)."""
    return secrets.token_hex(16)


class ReviseRecord(BaseModel):
    token: str
    owner_secret_local: str  # the secret-address local-part that created this record
    thread: ParsedThread
    origin_kind: ResultKind  # DRAFT records are revisable; SUMMARY is terminal
    revision_count: int = 0
    recipient_uncertain: bool = False
    # KL ("keep local") stickiness: a revise round-trip on a keep-local draft must stay on
    # the on-device model and never reach the Cloud API (fail-closed if no local backend).
    keep_local: bool = False
    created_at: float = Field(default_factory=time.time)


class PendingActionRecord(BaseModel):
    """A consent-gated action proposed to the CEO, awaiting confirmation (0.2.0).

    Keyed by a one-time correlation token (same generator + owner-binding as
    ``ReviseRecord``). ``status`` is the single-use guard: ``pending`` until the CEO
    confirms, then ``consumed`` — a second confirm is a no-op (never a second send).
    Lives in a SEPARATE store instance from revise records so a pending-action token
    and a revise token can never cross-route (KTD-B)."""

    token: str
    owner_secret_local: str  # the secret-address local-part that created this record
    recipient: str           # the third party the proposal email will be sent to
    subject: str
    body: str                # the exact Nora-voiced email the CEO approves verbatim
    status: str = "pending"  # "pending" -> "consumed"
    action_kind: str = "send_email"  # "send_email" (default) or "calendar_hold"
    payload: dict = Field(default_factory=dict)  # kind-specific extra data
    created_at: float = Field(default_factory=time.time)
    # Follow-up loop: how many reminders the principal has had for this un-AG'd proposal, and when.
    reminders_sent: int = 0
    last_reminded_at: float | None = None


class StateStore(Protocol):
    """Backend-agnostic store for revise records."""

    def put(self, record: ReviseRecord) -> None: ...
    def get(self, token: str) -> ReviseRecord | None: ...
    def delete(self, token: str) -> None: ...
    def purge(self) -> int: ...


class InMemoryStateStore:
    """In-memory store with TTL. Single-instance only (see U8 constraint)."""

    def __init__(self, ttl_seconds: int) -> None:
        self._ttl = ttl_seconds
        self._data: dict[str, ReviseRecord] = {}

    def put(self, record: ReviseRecord) -> None:
        self._data[record.token] = record

    def get(self, token: str) -> ReviseRecord | None:
        record = self._data.get(token)
        if record is None:
            return None
        if self._expired(record):
            self.delete(token)
            return None
        return record

    def delete(self, token: str) -> None:
        self._data.pop(token, None)

    def purge(self) -> int:
        stale = [t for t, r in self._data.items() if self._expired(r)]
        for t in stale:
            del self._data[t]
        return len(stale)

    def _expired(self, record: ReviseRecord, now: float | None = None) -> bool:
        now = now if now is not None else time.time()
        return (now - record.created_at) > self._ttl


class PendingActionStore(Protocol):
    """Backend-agnostic store for pending consent-gated actions (separate from the
    revise store — see KTD-B)."""

    def put(self, record: PendingActionRecord) -> None: ...
    def get(self, token: str) -> PendingActionRecord | None: ...
    def delete(self, token: str) -> None: ...
    def purge(self) -> int: ...
    def all_pending(self) -> list["PendingActionRecord"]: ...


class InMemoryPendingActionStore:
    """In-memory TTL store for pending actions. Single-instance only (U8 constraint).
    Deliberately a separate class/instance from ``InMemoryStateStore`` so the two token
    namespaces stay disjoint by construction (KTD-B)."""

    def __init__(self, ttl_seconds: int) -> None:
        self._ttl = ttl_seconds
        self._data: dict[str, PendingActionRecord] = {}

    def put(self, record: PendingActionRecord) -> None:
        self._data[record.token] = record

    def get(self, token: str) -> PendingActionRecord | None:
        record = self._data.get(token)
        if record is None:
            return None
        if self._expired(record):
            self.delete(token)
            return None
        return record

    def delete(self, token: str) -> None:
        self._data.pop(token, None)

    def purge(self) -> int:
        stale = [t for t, r in self._data.items() if self._expired(r)]
        for t in stale:
            del self._data[t]
        return len(stale)

    def all_pending(self) -> list[PendingActionRecord]:
        return [r for r in self._data.values() if r.status == "pending" and not self._expired(r)]

    def _expired(self, record: PendingActionRecord, now: float | None = None) -> bool:
        now = now if now is not None else time.time()
        return (now - record.created_at) > self._ttl


class FilePendingActionStore:
    """File-backed PendingActionStore: pending proposals survive a restart. Same TTL +
    single-use semantics as InMemoryPendingActionStore, over the shared JSON-map I/O.
    Writes are best-effort — a persistence failure logs and is swallowed so it never
    breaks the inbound reply (worst case the proposal isn't stored and the CEO re-forwards)."""

    def __init__(self, path: str, ttl_seconds: int) -> None:
        self._path = path
        self._ttl = ttl_seconds

    def _save(self, data: dict[str, dict]) -> None:
        try:
            atomic_write_json_map(self._path, data)
        except OSError:
            logger.exception("pending-action store write failed at %s (not persisted)", self._path)

    def put(self, record: PendingActionRecord) -> None:
        data = read_json_map(self._path)
        data[record.token] = record.model_dump()
        self._save(data)

    def _parse(self, raw: dict) -> PendingActionRecord | None:
        """Reconstruct a record, tolerating a malformed/schema-drifted one. A persistent
        store outlives deploys, so an old record that no longer fits the model must NOT
        crash an inbound (purge runs on every inbound) — drop it instead."""
        try:
            return PendingActionRecord(**raw)
        except Exception:  # noqa: BLE001 — a bad persisted record is dropped, never fatal
            logger.exception("pending-action store: dropping unparseable record at %s", self._path)
            return None

    def get(self, token: str) -> PendingActionRecord | None:
        raw = read_json_map(self._path).get(token)
        if raw is None:
            return None
        record = self._parse(raw)
        if record is None or self._expired(record):
            self.delete(token)  # expired or unparseable → self-heal
            return None
        return record

    def delete(self, token: str) -> None:
        data = read_json_map(self._path)
        if data.pop(token, None) is not None:
            self._save(data)

    def purge(self) -> int:
        data = read_json_map(self._path)
        # Drop expired AND unparseable records so the file self-heals across schema drift.
        drop = [t for t, raw in data.items() if (rec := self._parse(raw)) is None or self._expired(rec)]
        for t in drop:
            del data[t]
        if drop:
            self._save(data)
        return len(drop)

    def all_pending(self) -> list[PendingActionRecord]:
        out = []
        for raw in read_json_map(self._path).values():
            rec = self._parse(raw)
            if rec is not None and rec.status == "pending" and not self._expired(rec):
                out.append(rec)
        return out

    def _expired(self, record: PendingActionRecord, now: float | None = None) -> bool:
        now = now if now is not None else time.time()
        return (now - record.created_at) > self._ttl


class TriageSessionRecord(BaseModel):
    """The split items behind a triage digest (TR v2), keyed by the digest's correlation token so
    a reply like '2 RE' can act on item 2 without re-forwarding. Holds the CEO's OWN forwarded
    content transiently (same TTL + owner-binding as ReviseRecord); a SEPARATE store instance so a
    triage token never cross-routes with a revise or pending-action token (KTD-B)."""

    token: str
    owner_secret_local: str
    items: list[str]
    subject: str = ""
    created_at: float = Field(default_factory=time.time)


class TriageStore(Protocol):
    """Backend-agnostic store for triage-session records."""

    def put(self, record: TriageSessionRecord) -> None: ...
    def get(self, token: str) -> TriageSessionRecord | None: ...
    def delete(self, token: str) -> None: ...
    def purge(self) -> int: ...


class InMemoryTriageStore:
    """In-memory triage-session store with TTL. Single-instance only (see U8 constraint)."""

    def __init__(self, ttl_seconds: int) -> None:
        self._ttl = ttl_seconds
        self._data: dict[str, TriageSessionRecord] = {}

    def put(self, record: TriageSessionRecord) -> None:
        self._data[record.token] = record

    def get(self, token: str) -> TriageSessionRecord | None:
        record = self._data.get(token)
        if record is None:
            return None
        if self._expired(record):
            self.delete(token)
            return None
        return record

    def delete(self, token: str) -> None:
        self._data.pop(token, None)

    def purge(self) -> int:
        stale = [t for t, r in self._data.items() if self._expired(r)]
        for t in stale:
            del self._data[t]
        return len(stale)

    def _expired(self, record: TriageSessionRecord, now: float | None = None) -> bool:
        now = now if now is not None else time.time()
        return (now - record.created_at) > self._ttl


class FileTriageStore:
    """File-backed TriageStore: a digest's items survive a restart. Same TTL semantics over the
    shared JSON-map I/O; writes are best-effort (a failure logs and is swallowed — worst case the
    session isn't stored and the CEO re-forwards)."""

    def __init__(self, path: str, ttl_seconds: int) -> None:
        self._path = path
        self._ttl = ttl_seconds

    def _save(self, data: dict[str, dict]) -> None:
        try:
            atomic_write_json_map(self._path, data)
        except OSError:
            logger.exception("triage store write failed at %s (not persisted)", self._path)

    def _parse(self, raw: dict) -> TriageSessionRecord | None:
        try:
            return TriageSessionRecord(**raw)
        except Exception:  # noqa: BLE001 — a bad persisted record is dropped, never fatal
            logger.exception("triage store: dropping unparseable record at %s", self._path)
            return None

    def put(self, record: TriageSessionRecord) -> None:
        data = read_json_map(self._path)
        data[record.token] = record.model_dump()
        self._save(data)

    def get(self, token: str) -> TriageSessionRecord | None:
        raw = read_json_map(self._path).get(token)
        if raw is None:
            return None
        record = self._parse(raw)
        if record is None or self._expired(record):
            self.delete(token)
            return None
        return record

    def delete(self, token: str) -> None:
        data = read_json_map(self._path)
        if data.pop(token, None) is not None:
            self._save(data)

    def purge(self) -> int:
        data = read_json_map(self._path)
        drop = [t for t, raw in data.items() if (rec := self._parse(raw)) is None or self._expired(rec)]
        for t in drop:
            del data[t]
        if drop:
            self._save(data)
        return len(drop)

    def _expired(self, record: TriageSessionRecord, now: float | None = None) -> bool:
        now = now if now is not None else time.time()
        return (now - record.created_at) > self._ttl
