"""OpenRouter drafting backend — an OpenAI-compatible cloud alternative to the Claude
backend (e.g. for free/cheap models when an Anthropic key is unavailable). Same NoraResult
contract via the shared JSON-schema structured output (json_backend), over OpenRouter's
`/chat/completions`. Intended for dev/demo + non-production transports (see config guard)."""

from __future__ import annotations

import time
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

# Free OpenRouter providers are often momentarily saturated → a transient 429 (with a short
# Retry-After) or 502/503. Brief backoff-retry keeps drafting reliable enough to record on.
_RETRY_STATUSES = {429, 502, 503}


def _retry_after_seconds(resp: Any) -> float:
    headers = getattr(resp, "headers", None) or {}
    raw = headers.get("Retry-After") or headers.get("retry-after")
    try:
        return min(float(raw), 8.0) if raw else 2.0
    except (TypeError, ValueError):
        return 2.0


def chat_completion(
    client: Any, base_url: str, api_key: str, model: str, system: str, user: str,
    *, max_tokens: int, schema: dict | None = None, max_retries: int = 2,
) -> str:
    """One OpenRouter chat completion; returns the assistant text. `schema` (when given)
    requests strict json_schema structured output — the caller still parses defensively.
    Transient upstream rate-limits (429/502/503) are retried with a short backoff."""
    body: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.2,
        "max_tokens": max_tokens,
    }
    if schema is not None:
        body["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "nora", "strict": True, "schema": schema},
        }
    url = base_url.rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}"}
    for attempt in range(max_retries + 1):
        resp = client.post(url, headers=headers, json=body)
        if getattr(resp, "status_code", 200) in _RETRY_STATUSES and attempt < max_retries:
            time.sleep(_retry_after_seconds(resp))
            continue
        break
    resp.raise_for_status()
    choices = resp.json().get("choices") or []
    if not choices:
        return ""
    return (choices[0].get("message") or {}).get("content", "") or ""


class OpenRouterDraftingService:
    def __init__(
        self, base_url: str, api_key: str, model: str, timeout: float = 60.0, client: Any = None
    ) -> None:
        self._base_url = base_url
        self._api_key = api_key
        self._model = model
        self._client = client or httpx.Client(timeout=timeout)

    def _call(self, req: ParsedRequest, intent: Intent, *, use_schema: bool) -> dict | None:
        try:
            content = chat_completion(
                self._client, self._base_url, self._api_key, self._model,
                JSON_DRAFT_SYSTEM, build_user_content(req, intent),
                max_tokens=1500, schema=DRAFT_SCHEMA if use_schema else None,
            )
        except Exception:  # noqa: BLE001 — a transport/HTTP error retries, then fails typed
            return None
        return parse_json_content(content)

    def produce(self, req: ParsedRequest, intent: Intent) -> NoraResult:
        # First ask for strict json_schema (best structure on supporting models); the retry
        # DROPS response_format — OpenRouter *errors* when a model can't do structured output,
        # so the prompt-only fallback keeps free/unsupported models working.
        data = self._call(req, intent, use_schema=True)
        if data is None or not fields_valid(data):
            data = self._call(req, intent, use_schema=False)
        if data is None or not fields_valid(data):
            raise DraftingError("openrouter did not return valid structured output")
        if intent is Intent.DRAFT and isinstance(data.get("draft_or_summary"), str):
            data["draft_or_summary"] = strip_trailing_name_placeholder(data["draft_or_summary"])
        return result_from_fields(data, req, intent)
