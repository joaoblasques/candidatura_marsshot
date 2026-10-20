"""Relationship memory — Layer C directive parsing (CEO-curated facts).

A CEO curates a durable fact about a counterparty with ``[remember: <fact>]`` on a thread, or
clears them with ``[forget]``. This module only parses the directive out of the instruction text;
storage lives in app/memory.py and is recalled into the draft on the R1 path (app/pipeline.py).
Nothing is auto-derived from message content in v1 — only what the CEO explicitly states."""

from __future__ import annotations

import re

# Line-scoped (no DOTALL): a directive occupies its own bracket note (its own line, since the
# parser keeps each [bracket] on a separate line), so the fact stops at the end of that line and
# can't absorb a sibling note like [warmly]. _FORGET matches a standalone 'forget' line (MULTILINE)
# so [forget] still works when combined with other brackets.
_REMEMBER = re.compile(r"\bremember\s*:\s*([^\n]+)", re.IGNORECASE)
_FORGET = re.compile(r"(?im)^\s*forget(?:\s+(?:this\s+)?(?:contact|counterparty))?\s*$")

# A stored fact is bounded so the draft context (and the file) can't grow unboundedly.
MAX_FACT_LEN = 280


def parse_relationship_directive(instruction_text: str | None) -> tuple[str | None, str | None, str]:
    """Return ``(action, fact, remaining)``.

    - ``[remember: <fact>]`` → ``("remember", "<fact>", <instruction without the directive>)``.
    - ``[forget]`` / ``[forget contact]`` → ``("forget", None, "")``.
    - anything else → ``(None, None, <instruction unchanged>)``.

    The directive is stripped from ``remaining`` so the rest of the instruction still drives the
    draft (e.g. ``[warmly] [remember: prefers mornings]`` still drafts warmly)."""
    text = (instruction_text or "").strip()
    if not text:
        return None, None, ""
    fm = _FORGET.search(text)
    if fm:
        # remaining = everything except the matched 'forget' line, so sibling notes survive.
        remaining = (text[: fm.start()] + text[fm.end():]).strip()
        return "forget", None, remaining
    m = _REMEMBER.search(text)
    if m:
        fact = m.group(1).strip()[:MAX_FACT_LEN].strip()
        # remaining = the instruction with the 'remember: …' directive removed (before + after).
        remaining = (text[: m.start()] + text[m.end():]).strip()
        return ("remember", fact, remaining) if fact else (None, None, remaining)
    return None, None, text
