"""Local (Ollama) drafting backend — dev-only. Same NoraResult contract as the Claude
backend, via Ollama /api/chat structured output (JSON schema) instead of tool-use, so an
8B local model returns the fields reliably. Reaches no external network (R3 direction)."""

from __future__ import annotations

from typing import Any

import httpx

from app.drafting.json_backend import (
    DRAFT_SCHEMA,
    JSON_DRAFT_SYSTEM,
    parse_json_content,
    strip_trailing_name_placeholder,
)
from app.drafting.prompts import build_user_content
from app.drafting.result import DraftingError, fields_valid, result_from_fields
from app.models import Intent, NoraResult, ParsedRequest


class OllamaDraftingService:
    def __init__(self, base_url: str, model: str, timeout: float = 120.0, client: Any = None) -> None:
        self._url = base_url.rstrip("/") + "/api/chat"
        self._model = model
        self._client = client or httpx.Client(timeout=timeout)

    def _call(self, req: ParsedRequest, intent: Intent) -> dict | None:
        resp = self._client.post(
            self._url,
            json={
                "model": self._model,
                "messages": [
                    {"role": "system", "content": JSON_DRAFT_SYSTEM},
                    {"role": "user", "content": build_user_content(req, intent)},
                ],
                "stream": False,
                "think": False,
                "format": DRAFT_SCHEMA,
                "options": {"temperature": 0.2},
            },
        )
        resp.raise_for_status()
        content = (resp.json().get("message") or {}).get("content", "")
        return parse_json_content(content)

    def produce(self, req: ParsedRequest, intent: Intent) -> NoraResult:
        data = self._call(req, intent)
        if data is None or not fields_valid(data):
            data = self._call(req, intent)  # one retry
        if data is None or not fields_valid(data):
            raise DraftingError("ollama did not return valid structured output")
        if intent is Intent.DRAFT and isinstance(data.get("draft_or_summary"), str):
            data["draft_or_summary"] = strip_trailing_name_placeholder(data["draft_or_summary"])
        return result_from_fields(data, req, intent)
