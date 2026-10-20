"""U1: app boots and serves health only when configuration is valid."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def test_health_ok(bootable_settings: Settings) -> None:
    client = TestClient(create_app(bootable_settings))
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_boot_gate_requires_operator_ack() -> None:
    settings = Settings(
        external_llm_ack=False,
        anthropic_api_key="k",
        provider_api_key="k",
        webhook_basic_auth="k",
    )
    with pytest.raises(RuntimeError, match="EXTERNAL_LLM_ACK"):
        create_app(settings)


def test_boot_gate_requires_credentials() -> None:
    settings = Settings(external_llm_ack=True)  # ack set, but no secrets
    with pytest.raises(RuntimeError, match="Missing required configuration"):
        create_app(settings)
