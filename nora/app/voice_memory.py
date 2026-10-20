"""Layer B — per-person VOICE memory. Stores ONLY the voice-relevant refinement codes the CEO
uses (F/I/Long/Short/MN) + timestamps — never message content, never free text. So there is
nothing sensitive to re-leak and no injection sink (the dream consumes enum codes, not prose).
R1-only by construction (the pipeline records/reads only on the R1 path). Default Null (off)."""

from __future__ import annotations

from collections import Counter
from typing import Protocol

from app.state.json_file_map import atomic_write_json_map, read_json_map

VOICE_CODES = {"F", "I", "LONG", "SHORT", "MN"}


class VoiceStore(Protocol):
    def record(self, ceo_local: str, codes: list[str], when: float, counterparty: str = "") -> None: ...
    def signals(self, ceo_local: str, counterparty: str = "") -> list[dict]: ...
    def profile(self, ceo_local: str, counterparty: str = "") -> str: ...
    def set_profile(self, ceo_local: str, text: str, counterparty: str = "") -> None: ...
    def forget(self, ceo_local: str) -> None: ...


class NullVoiceStore:
    """Default: voice memory off — record no-ops, profile empty."""

    def record(self, ceo_local: str, codes: list[str], when: float, counterparty: str = "") -> None:
        return None

    def signals(self, ceo_local: str, counterparty: str = "") -> list[dict]:
        return []

    def profile(self, ceo_local: str, counterparty: str = "") -> str:
        return ""

    def set_profile(self, ceo_local: str, text: str, counterparty: str = "") -> None:
        return None

    def forget(self, ceo_local: str) -> None:
        return None


def _key(ceo_local: str, counterparty: str = "") -> str:
    """Storage key. Global = ``ceo`` (Layer B v1, unchanged); per-recipient = ``ceo|cp``
    (Layer B v2). Counterparties are email addresses and CEO locals are hash/registry tokens,
    neither of which contains ``|`` — so the separator is unambiguous."""
    ceo = (ceo_local or "").strip().lower()
    cp = (counterparty or "").strip().lower()
    return f"{ceo}|{cp}" if cp else ceo


class FileVoiceStore:
    """Per-CEO voice signals + a consolidated profile, in one atomic JSON map keyed by ceo_local.
    Value: {"signals": [{"code", "when"}], "profile": str}. Bounded per CEO."""

    def __init__(self, path: str, *, max_signals: int = 200) -> None:
        self._path = path
        self._max = max_signals

    def _entry(self, data: dict, ceo: str) -> dict:
        e = data.get(ceo)
        return e if isinstance(e, dict) else {"signals": [], "profile": ""}

    def record(self, ceo_local: str, codes: list[str], when: float, counterparty: str = "") -> None:
        key = _key(ceo_local, counterparty)
        if not key:
            return
        voice = [c for c in (codes or []) if c in VOICE_CODES]
        if not voice:
            return
        data = read_json_map(self._path)
        entry = self._entry(data, key)
        sigs = list(entry.get("signals", [])) + [{"code": c, "when": when} for c in voice]
        entry["signals"] = sigs[-self._max:]  # bound growth — keep the most recent
        data[key] = entry
        atomic_write_json_map(self._path, data)

    def signals(self, ceo_local: str, counterparty: str = "") -> list[dict]:
        entry = self._entry(read_json_map(self._path), _key(ceo_local, counterparty))
        return [s for s in entry.get("signals", []) if isinstance(s, dict) and s.get("code")]

    def profile(self, ceo_local: str, counterparty: str = "") -> str:
        return str(self._entry(read_json_map(self._path), _key(ceo_local, counterparty)).get("profile", ""))

    def set_profile(self, ceo_local: str, text: str, counterparty: str = "") -> None:
        key = _key(ceo_local, counterparty)
        if not key:
            return
        data = read_json_map(self._path)
        entry = self._entry(data, key)
        entry["profile"] = text
        data[key] = entry
        atomic_write_json_map(self._path, data)

    def forget(self, ceo_local: str) -> None:
        """Wipe ALL of a CEO's voice — the global key and every per-recipient ``ceo|cp`` key."""
        ceo = _key(ceo_local)
        if not ceo:
            return
        data = read_json_map(self._path)
        doomed = [k for k in data if k == ceo or k.startswith(f"{ceo}|")]
        if doomed:
            for k in doomed:
                del data[k]
            atomic_write_json_map(self._path, data)


_LABELS = {"SHORT": "concise", "LONG": "detailed", "F": "formal", "I": "informal",
           "MN": "written in your name"}
_PAIRS = [("SHORT", "LONG"), ("F", "I")]  # opposed dials — keep the more frequent


def build_voice_profile(signals: list[dict], now: float, *, ttl_seconds: float) -> str:
    counts = Counter(
        s["code"]
        for s in signals
        if isinstance(s, dict) and s.get("code") in _LABELS and (now - s.get("when", 0)) <= ttl_seconds
    )
    if not counts:
        return ""
    chosen: list[str] = []
    used = set()
    for a, b in _PAIRS:
        if counts[a] or counts[b]:
            chosen.append(a if counts[a] >= counts[b] else b)
            used.update({a, b})
    for code in counts:  # any remaining single-sided dials (e.g. MN)
        if code not in used:
            chosen.append(code)
    order = ["SHORT", "LONG", "F", "I", "MN"]  # stable, readable order
    labels = [_LABELS[c] for c in order if c in chosen]
    return "Preferred style: " + ", ".join(labels) + "."


def consolidate_store(store, ceo_local: str, now: float, *, ttl_seconds: float) -> str:
    live = [s for s in store.signals(ceo_local) if (now - s.get("when", 0)) <= ttl_seconds]
    profile = build_voice_profile(live, now, ttl_seconds=ttl_seconds)
    if hasattr(store, "_path"):  # FileVoiceStore: rewrite signals to the live set + set profile
        data = read_json_map(store._path)
        entry = data.get(_key(ceo_local))
        if isinstance(entry, dict):
            entry["signals"] = live
            entry["profile"] = profile
            data[_key(ceo_local)] = entry
            atomic_write_json_map(store._path, data)
    else:
        store.set_profile(ceo_local, profile)
    return profile
