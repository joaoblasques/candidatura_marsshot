"""Per-sender inbox-action rules (owner-scoped). "Every command auto-becomes a rule" — future
mail from that sender gets the same safe actions applied by the poller. Stored as a JSON map keyed
``owner|sender`` → list of canonical action strings (app/inbox_actions.py). Same atomic, corrupt-
safe I/O as the other file stores. Default NullInboxRuleStore (off)."""

from __future__ import annotations

from typing import Protocol

from app.state.json_file_map import atomic_write_json_map, read_json_map


class InboxRuleStore(Protocol):
    def get(self, owner: str, sender: str) -> list[str] | None: ...
    def set(self, owner: str, sender: str, actions: list[str]) -> None: ...
    def clear(self, owner: str, sender: str) -> None: ...
    def all(self, owner: str) -> dict[str, list[str]]: ...


class NullInboxRuleStore:
    """Default: rules off — get finds nothing, set/clear no-op, all empty."""

    def get(self, owner: str, sender: str) -> list[str] | None:
        return None

    def set(self, owner: str, sender: str, actions: list[str]) -> None:
        return None

    def clear(self, owner: str, sender: str) -> None:
        return None

    def all(self, owner: str) -> dict[str, list[str]]:
        return {}


def _key(owner: str, sender: str) -> str:
    return f"{owner.strip().lower()}|{sender.strip().lower()}"


class FileInboxRuleStore:
    """Per-owner per-sender action rules in an atomic JSON map."""

    def __init__(self, path: str) -> None:
        self._path = path

    def get(self, owner: str, sender: str) -> list[str] | None:
        if not owner or not sender:
            return None
        rec = read_json_map(self._path).get(_key(owner, sender))
        return [str(a) for a in rec] if isinstance(rec, list) and rec else None

    def set(self, owner: str, sender: str, actions: list[str]) -> None:
        actions = [str(a) for a in (actions or []) if str(a).strip()]
        if not owner or not sender or not actions:
            return
        data = read_json_map(self._path)
        data[_key(owner, sender)] = actions
        atomic_write_json_map(self._path, data)

    def clear(self, owner: str, sender: str) -> None:
        if not owner or not sender:
            return
        data = read_json_map(self._path)
        if data.pop(_key(owner, sender), None) is not None:
            atomic_write_json_map(self._path, data)

    def all(self, owner: str) -> dict[str, list[str]]:
        prefix = f"{owner.strip().lower()}|"
        out: dict[str, list[str]] = {}
        for k, v in read_json_map(self._path).items():
            if k.startswith(prefix) and isinstance(v, list):
                out[k[len(prefix):]] = [str(a) for a in v]
        return out
