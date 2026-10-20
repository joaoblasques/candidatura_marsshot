"""Draft-feedback learning: track auto-drafts and learn from what the CEO does with them. A draft
that's deleted (not sent) is a negative signal; after ``_SUPPRESS_AT`` deletions Nora stops auto-
drafting that sender. A sent draft resets it. Owner-scoped JSON, same atomic I/O as the other file
stores. Default NullDraftFeedbackStore (off). Reduces-only, reversible."""

from __future__ import annotations

from typing import Protocol

from app.state.json_file_map import atomic_write_json_map, read_json_map

_SUPPRESS_AT = 2  # deletions before Nora stops auto-drafting a sender (a pattern, not a one-off)


class DraftFeedbackStore(Protocol):
    def track(self, owner: str, draft_id: str, sender: str, thread_id: str) -> None: ...
    def pending(self, owner: str) -> dict[str, tuple[str, str]]: ...
    def resolve(self, owner: str, draft_id: str) -> None: ...
    def record_deletion(self, owner: str, sender: str) -> int: ...
    def record_sent(self, owner: str, sender: str) -> None: ...
    def suppressed(self, owner: str, sender: str) -> bool: ...
    def stats(self, owner: str) -> dict[str, object]: ...


class NullDraftFeedbackStore:
    """Default: learning off — track/resolve/record no-op, nothing pending, never suppressed."""

    def track(self, owner: str, draft_id: str, sender: str, thread_id: str) -> None:
        return None

    def pending(self, owner: str) -> dict[str, tuple[str, str]]:
        return {}

    def resolve(self, owner: str, draft_id: str) -> None:
        return None

    def record_deletion(self, owner: str, sender: str) -> int:
        return 0

    def record_sent(self, owner: str, sender: str) -> None:
        return None

    def suppressed(self, owner: str, sender: str) -> bool:
        return False

    def stats(self, owner: str) -> dict[str, object]:
        return {"created": 0, "sent": 0, "deleted": 0, "acceptance_rate": None}


def _pk(owner: str, draft_id: str) -> str:
    return f"pending|{owner.strip().lower()}|{draft_id}"


def _sk(owner: str, sender: str) -> str:
    return f"sender|{owner.strip().lower()}|{sender.strip().lower()}"


def _statk(owner: str) -> str:
    return f"stats|{owner.strip().lower()}"


def _bump(data: dict, owner: str, field: str) -> None:
    key = _statk(owner)
    rec = data.get(key) if isinstance(data.get(key), dict) else {}
    data[key] = {**{"created": 0, "sent": 0, "deleted": 0}, **rec}
    data[key][field] = int(data[key].get(field, 0)) + 1


class FileDraftFeedbackStore:
    def __init__(self, path: str) -> None:
        self._path = path

    def track(self, owner: str, draft_id: str, sender: str, thread_id: str) -> None:
        if not (owner and draft_id and sender):
            return
        data = read_json_map(self._path)
        data[_pk(owner, draft_id)] = {"sender": sender.strip().lower(), "thread": thread_id or ""}
        _bump(data, owner, "created")
        atomic_write_json_map(self._path, data)

    def pending(self, owner: str) -> dict[str, tuple[str, str]]:
        prefix = f"pending|{owner.strip().lower()}|"
        out: dict[str, tuple[str, str]] = {}
        for k, v in read_json_map(self._path).items():
            if k.startswith(prefix) and isinstance(v, dict):
                out[k[len(prefix):]] = (str(v.get("sender", "")), str(v.get("thread", "")))
        return out

    def resolve(self, owner: str, draft_id: str) -> None:
        data = read_json_map(self._path)
        if data.pop(_pk(owner, draft_id), None) is not None:
            atomic_write_json_map(self._path, data)

    def record_deletion(self, owner: str, sender: str) -> int:
        if not owner or not sender:
            return 0
        data = read_json_map(self._path)
        key = _sk(owner, sender)
        rec = data.get(key) if isinstance(data.get(key), dict) else {}
        n = int(rec.get("deletions", 0)) + 1
        data[key] = {"deletions": n, "suppressed": n >= _SUPPRESS_AT}
        _bump(data, owner, "deleted")
        atomic_write_json_map(self._path, data)
        return n

    def record_sent(self, owner: str, sender: str) -> None:
        if not owner or not sender:
            return
        data = read_json_map(self._path)
        key = _sk(owner, sender)
        if data.get(key):
            data[key] = {"deletions": 0, "suppressed": False}
        _bump(data, owner, "sent")  # count every send, even without a prior deletion
        atomic_write_json_map(self._path, data)

    def suppressed(self, owner: str, sender: str) -> bool:
        rec = read_json_map(self._path).get(_sk(owner, sender))
        return bool(rec.get("suppressed")) if isinstance(rec, dict) else False

    def stats(self, owner: str) -> dict[str, object]:
        rec = read_json_map(self._path).get(_statk(owner))
        c = rec if isinstance(rec, dict) else {}
        created, sent, deleted = (int(c.get(k, 0)) for k in ("created", "sent", "deleted"))
        decided = sent + deleted
        rate = (sent / decided) if decided else None
        return {"created": created, "sent": sent, "deleted": deleted, "acceptance_rate": rate}
