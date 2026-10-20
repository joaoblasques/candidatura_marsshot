"""Idle 'dream' for voice memory: consolidate each CEO's coded signals into a profile + decay
stale ones. Deterministic (no LLM, no content) — run nightly by a system cron. Bounded single-pass."""

from __future__ import annotations

import sys
import time

from app.logging import configure_logging, get_logger
from app.state.json_file_map import read_json_map
from app.voice_memory import consolidate_store

logger = get_logger(__name__)


def consolidate_all(store, ceo_locals, now: float, *, ttl_seconds: float) -> int:
    done = 0
    for ceo in ceo_locals:
        consolidate_store(store, ceo, now, ttl_seconds=ttl_seconds)
        done += 1
    return done


def main(argv: list[str]) -> int:
    from app.config import get_settings
    from app.voice_memory import FileVoiceStore

    configure_logging()
    settings = get_settings()
    if not settings.voice_memory:
        logger.info("voice memory off (NORA_VOICE_MEMORY) — nothing to consolidate")
        return 0
    store = FileVoiceStore(settings.voice_path, max_signals=settings.voice_max_signals)
    ceos = list(read_json_map(settings.voice_path).keys())
    n = consolidate_all(store, ceos, time.time(), ttl_seconds=settings.voice_signal_ttl_seconds)
    logger.info("voice dream: consolidated %d profile(s)", n)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
