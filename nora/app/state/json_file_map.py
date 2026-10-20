"""Shared atomic, corrupt-safe JSON-map file I/O for the file-backed stores
(FileSendOutbox, FilePendingActionStore). A map is token -> record-dict."""

from __future__ import annotations

import json
import os

from app.logging import get_logger

logger = get_logger(__name__)


def read_json_map(path: str) -> dict[str, dict]:
    """Load a token->record map. Missing file -> {}. Corrupt/unreadable -> log + {}
    (never raises) so a bad file never crashes boot or an inbound."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, OSError):
        logger.exception("json map unreadable at %s — treating as empty", path)
        return {}


def atomic_write_json_map(path: str, data: dict[str, dict]) -> None:
    """Write atomically (temp file + os.replace) so a crash never leaves a partial file."""
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.replace(tmp, path)
