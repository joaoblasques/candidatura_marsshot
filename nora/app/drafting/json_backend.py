"""Shared structured-output helpers for non-tool-use drafting backends (Ollama local,
OpenRouter cloud). Both ask a model for the SAME field set as the Claude tool — via a
JSON schema + "respond with ONLY JSON" instruction — then parse the text back. Keeping
the prompt, schema, placeholder guard, and parser here means one source of truth so the
two backends can't drift from the Claude contract."""

from __future__ import annotations

import json

from app.drafting.prompts import BASE_SYSTEM_PROMPT, build_tool

# The Claude tool's input_schema verbatim — it carries the field DESCRIPTIONS (esp.
# draft_or_summary's "never a label/code/task name") that a non-Claude model needs.
DRAFT_SCHEMA = build_tool()["input_schema"]

# Lean system prompt: BASE (no @-code grammar — see prompts.py) + an explicit JSON-only
# instruction. Smaller/open models loop the code grammar into `recipient`, so it's dropped.
JSON_DRAFT_SYSTEM = (
    BASE_SYSTEM_PROMPT
    + "\nRespond with ONLY a single JSON object holding draft_or_summary, "
    "interpretation_readback, recipient, and uncertainty. draft_or_summary MUST be the "
    "actual email/summary text — never the task name, a code, or a placeholder."
)

# Open models sometimes append a name/role placeholder after the sign-off (a bare "CEO"
# or "[Your Name]") despite the prompt. Strip a TRAILING line that is *solely* one of
# these — deliberately narrow (final non-empty line, exact match) so it can't eat body text.
_NAME_PLACEHOLDERS = frozenset({
    "ceo", "[ceo]", "[ceo name]", "ceo name", "[your name]", "your name",
    "[name]", "name", "[signature]", "[your title]", "[title]", "[your signature]",
})


def strip_trailing_name_placeholder(text: str) -> str:
    lines = text.rstrip().split("\n")
    if not lines:
        return text
    last = lines[-1].strip().strip(".,").lower()
    if last in _NAME_PLACEHOLDERS:
        return "\n".join(lines[:-1]).rstrip()
    return text


def _loads(s: str) -> dict | None:
    try:
        data = json.loads(s)
    except (json.JSONDecodeError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def parse_json_content(content: str | None) -> dict | None:
    """Parse a model's text reply into a dict — tolerant of ```json fences and surrounding
    prose (open models wrap or annotate JSON). Returns None if no JSON object is found."""
    if not content or not isinstance(content, str):
        return None
    text = content.strip()
    if text.startswith("```"):  # drop a leading ```/```json fence and trailing ```
        nl = text.find("\n")
        text = text[nl + 1:] if nl != -1 else ""
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
        text = text.strip()
    data = _loads(text)
    if data is None:  # last resort: the outermost {...} span
        i, j = text.find("{"), text.rfind("}")
        if 0 <= i < j:
            data = _loads(text[i:j + 1])
    return data
