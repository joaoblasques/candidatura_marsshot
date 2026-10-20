"""Durable outbox for the one consent-gated third-party send (closes KTD-D).

A CEO-confirmed send is consumed before it is sent; a crash in that window would
otherwise drop it silently. The outbox persists the in-flight send so it can be
replayed at boot. Keyed by the action token; the provider send is idempotency-keyed
on the same token, so replay never produces a duplicate third-party email.
"""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel

from app.state.json_file_map import atomic_write_json_map, read_json_map


class OutboxRecord(BaseModel):
    """Everything needed to complete a confirmed send + its CEO receipt WITHOUT the
    original inbound email (so a fresh process can replay it at boot)."""

    token: str
    recipient: str
    subject: str
    body: str
    ceo_address: str
    ceo_sender: str | None = None  # per-person From for the CEO receipt (_sender_for)
    ceo_id: str                    # secret-address local part, for the redacted metric
    event_id: str                  # for redacted logs
    file_ref: str | None = None    # consent-gated file share: re-fetched + attached at send
    share_mode: str = "attach"     # "attach" (email the file) | "grant" (grant store access + link)


class SendOutbox(Protocol):
    def record(self, entry: OutboxRecord) -> None: ...
    def remove(self, token: str) -> None: ...
    def pending(self) -> list[OutboxRecord]: ...


class InMemorySendOutbox:
    """Non-durable outbox (default wiring + tests). Loses records on restart — used
    where durability is not required; prod uses FileSendOutbox."""

    def __init__(self) -> None:
        self._data: dict[str, OutboxRecord] = {}

    def record(self, entry: OutboxRecord) -> None:
        self._data[entry.token] = entry

    def remove(self, token: str) -> None:
        self._data.pop(token, None)

    def pending(self) -> list[OutboxRecord]:
        return list(self._data.values())


class FileSendOutbox:
    """Durable, single-instance outbox backed by one JSON file (a token->record map).
    Uses the shared atomic, corrupt-safe JSON-map I/O so the logic can't diverge from
    the pending-action store."""

    def __init__(self, path: str) -> None:
        self._path = path

    def record(self, entry: OutboxRecord) -> None:
        data = read_json_map(self._path)
        data[entry.token] = entry.model_dump()
        atomic_write_json_map(self._path, data)

    def remove(self, token: str) -> None:
        data = read_json_map(self._path)
        if data.pop(token, None) is not None:
            atomic_write_json_map(self._path, data)

    def pending(self) -> list[OutboxRecord]:
        return [OutboxRecord(**v) for v in read_json_map(self._path).values()]
