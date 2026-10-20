"""Ad-hoc R3 local-model voice/quality eval.

Runs the REAL local Ollama backend (the one R3 relationships use) across the
producing intents on representative threads and prints each draft for human
assessment. Not a pytest gate — a hands-on check before activating R3 locally.

Usage (on the Mini, where Ollama is local):
    uv run python scripts/eval_r3_local.py
"""

from __future__ import annotations

import os
import time

from app.drafting.ollama import OllamaDraftingService
from app.models import Intent, ParsedRequest, ParsedThread

BASE = os.environ.get("NORA_OLLAMA_BASE_URL", "http://localhost:11434")
MODEL = os.environ.get("NORA_OLLAMA_MODEL", "qwen3:8b")
TIMEOUT = float(os.environ.get("NORA_OLLAMA_TIMEOUT", "300"))

THREAD = ParsedThread(
    original_sender="sarah@acme.com",
    subject="Q3 numbers + next steps",
    body=(
        "From: Sarah Lee <sarah@acme.com>\n"
        "To: CEO <ceo@example.com>\n\n"
        "Hi — following up on our call. Could you send over the Q3 revenue numbers "
        "and let me know if Thursday 2pm works for the partnership review? We'd also "
        "like your read on whether the pilot scope still makes sense given the delays."
    ),
)

SCENARIOS = [
    (Intent.DRAFT, "Reply (RE): a warm, concise reply that addresses Sarah's asks."),
    (Intent.SUMMARIZE, "Summarize (S): a crisp summary of the thread for the CEO."),
    (Intent.ANALYZE, "Analyze (AC): Nora's read on the situation / what matters."),
]


def main() -> None:
    svc = OllamaDraftingService(BASE, MODEL, timeout=TIMEOUT)
    print(f"== R3 local eval — model={MODEL} base={BASE} ==\n")
    for intent, label in SCENARIOS:
        t0 = time.monotonic()
        result = svc.produce(ParsedRequest(thread=THREAD), intent)
        dt = time.monotonic() - t0
        print(f"--- {intent.value} ({label}) — {dt:.1f}s, kind={result.kind.value} ---")
        print(f"[readback] {result.interpretation_readback.strip()}")
        print(result.text.strip())
        print()


if __name__ == "__main__":
    main()
