"""Runtime-mutable user registry for R1 self-serve onboarding. Maps a provisioned secret
local-part -> the owner's verified From address(es), in one atomic JSON map (same I/O as the
other state stores). Merged with the static ceo_registry by effective_registry(); default Null
(off)."""

from __future__ import annotations

from typing import Protocol

from app.state.json_file_map import atomic_write_json_map, read_json_map


class UserStore(Protocol):
    def add_user(self, secret_local: str, from_addr: str) -> None: ...
    def find_by_sender(self, from_addr: str) -> str | None: ...
    def all(self) -> dict[str, list[str]]: ...


class NullUserStore:
    """Default: onboarding off — no dynamic users."""

    def add_user(self, secret_local: str, from_addr: str) -> None:
        return None

    def find_by_sender(self, from_addr: str) -> str | None:
        return None

    def all(self) -> dict[str, list[str]]:
        return {}


class FileUserStore:
    """File-backed dynamic registry: secret_local -> [from_addr]. Lowercased on write/read."""

    def __init__(self, path: str) -> None:
        self._path = path

    def all(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for k, v in read_json_map(self._path).items():
            if isinstance(v, list):
                out[k] = [str(x).strip().lower() for x in v if str(x).strip()]
            elif isinstance(v, str) and v.strip():
                out[k] = [v.strip().lower()]
        return out

    def add_user(self, secret_local: str, from_addr: str) -> None:
        data = read_json_map(self._path)
        data[secret_local] = [from_addr.strip().lower()]
        atomic_write_json_map(self._path, data)

    def find_by_sender(self, from_addr: str) -> str | None:
        fa = (from_addr or "").strip().lower()
        if not fa:
            return None
        for k, addrs in self.all().items():
            if fa in addrs:
                return k
        return None


def effective_registry(static_registry, user_store) -> dict:
    """The static env registry merged with the dynamic users. Static wins on collision (the
    dynamic tokens are unique, so collisions shouldn't occur)."""
    merged: dict = dict(static_registry or {})
    if user_store is not None:
        for k, v in user_store.all().items():
            merged.setdefault(k, v)
    return merged
