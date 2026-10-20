"""Parse a Nora inbox-action command (e.g. "star and archive", "label as ProjectX and archive")
into canonical, JSON-serializable action strings. SAFE/REVERSIBLE actions only — trash/delete are
never produced. Pure; no I/O. Actions are applied by a MailboxProvider (app/mailbox/provider.py)
and stored as per-sender rules (app/inbox_rules.py)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Canonical action strings. "label:<NAME>" carries the label/category name.
_LABEL_RE = re.compile(r"\b(?:label(?:\s+as)?|tag(?:\s+as)?)\s+([A-Za-z0-9][\w /-]*?)(?=\s*(?:,|;|\.|and\b|$))", re.IGNORECASE)
# Verb → action. Order-independent; matched as whole words. "file" is a common synonym for archive.
_VERBS = [
    (re.compile(r"\barchive\b|\bfile\b", re.IGNORECASE), "archive"),
    (re.compile(r"\bstar\b|\bflag\b", re.IGNORECASE), "star"),
    (re.compile(r"\b(?:mark\s+)?important\b", re.IGNORECASE), "important"),
    (re.compile(r"\b(?:mark\s+(?:as\s+)?)?unread\b", re.IGNORECASE), "unread"),
    (re.compile(r"\b(?:mark\s+(?:as\s+)?)?read\b", re.IGNORECASE), "read"),
]
_ONCE = re.compile(r"\b(?:once|just once|one[- ]time|this one|only this)\b", re.IGNORECASE)
_STOP = re.compile(r"\b(?:stop|no rule|forget rule|un-?rule)\b", re.IGNORECASE)
_LIST = re.compile(r"\brules\?|\bwhat rules\b|\blist rules\b|\bmy rules\b", re.IGNORECASE)


@dataclass
class ParsedCommand:
    actions: list[str] = field(default_factory=list)
    scope: str = "rule"          # "rule" (default — also become a per-sender rule) or "once"
    control: str | None = None   # None | "stop" (remove rule) | "list" (read back rules)


def parse_command(text: str) -> ParsedCommand:
    t = text or ""
    if _LIST.search(t):
        return ParsedCommand(control="list")
    if _STOP.search(t):
        return ParsedCommand(control="stop")
    # Collect (position, action) so the result follows the ORDER THE USER TYPED, then dedupe.
    hits: list[tuple[int, str]] = []
    for m in _LABEL_RE.finditer(t):
        hits.append((m.start(), f"label:{m.group(1).strip()}"))
    for pat, action in _VERBS:
        m = pat.search(t)
        if m:
            hits.append((m.start(), action))
    hits.sort(key=lambda h: h[0])
    seen: set[str] = set()
    ordered = [a for _, a in hits if not (a in seen or seen.add(a))]
    scope = "once" if _ONCE.search(t) else "rule"
    return ParsedCommand(actions=ordered, scope=scope)
