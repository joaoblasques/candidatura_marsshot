"""Claude drafting & summarizing service (U5).

Wraps the Anthropic client with a forced tool call for structured output (KTD3),
retries once on malformed output, then raises a typed error so the caller can
send a graceful "couldn't draft" receipt. The Anthropic client is injected so
the service is unit-testable without network access. Recipient anomaly check
(KTD9) is delegated to app.drafting.result so all backends share one guard.
"""

from __future__ import annotations

from typing import Any, Protocol

from app.drafting.prompts import (
    SYSTEM_PROMPT,
    TOOL_NAME,
    build_tool,
    build_user_content,
)
from app.drafting.result import DraftingError, fields_valid, result_from_fields  # noqa: F401 — re-export
from app.models import Intent, NoraResult, ParsedRequest

MAX_TOKENS = 1500


class MessagesClient(Protocol):
    """The Anthropic Messages surface DraftingService depends on (KTD3)."""

    @property
    def messages(self) -> Any: ...


def _extract_tool_input(response: object) -> dict | None:
    """Pull the forced tool-call input dict out of a Messages API response."""
    content = getattr(response, "content", None) or []
    for block in content:
        if getattr(block, "type", None) == "tool_use" and getattr(block, "name", "") == TOOL_NAME:
            data = getattr(block, "input", None)
            if isinstance(data, dict):
                return data
    return None


class DraftingService:
    def __init__(self, client: MessagesClient, model: str) -> None:
        self._client = client
        self._model = model

    def _call(self, req: ParsedRequest, intent: Intent) -> dict | None:
        response = self._client.messages.create(
            model=self._model,
            max_tokens=MAX_TOKENS,
            system=[
                {"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}
            ],
            tools=[build_tool(req.autolearn)],
            tool_choice={"type": "tool", "name": TOOL_NAME},
            messages=[{"role": "user", "content": build_user_content(req, intent)}],
        )
        return _extract_tool_input(response)

    def produce(self, req: ParsedRequest, intent: Intent) -> NoraResult:
        """Produce a draft (DRAFT) or summary (SUMMARIZE) as a NoraResult.

        Retries once on malformed output, then raises DraftingError.
        """
        data = self._call(req, intent)
        if data is None or not fields_valid(data):
            data = self._call(req, intent)  # one retry
        if data is None or not fields_valid(data):
            raise DraftingError("model did not return valid structured output")
        return result_from_fields(data, req, intent)
