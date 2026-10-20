from __future__ import annotations

from app.config import Settings
from app.posture import Posture, Rung, resolve_posture


def _settings(posture_map=None) -> Settings:
    return Settings(
        external_llm_ack=True, anthropic_api_key="k", provider_api_key="k",
        webhook_basic_auth="u:p", postmark_sender="n@x.com",
        ceo_registry={"h": "c@x.com"}, posture_map=posture_map or {},
    )


def test_default_is_r1() -> None:
    assert resolve_posture("sarah@acme.com", _settings()) == Posture(rung=Rung.R1)
    assert resolve_posture(None, _settings()).rung is Rung.R1


def test_exact_address_match() -> None:
    s = _settings({"vip@bigbank.com": "R3"})
    assert resolve_posture("vip@bigbank.com", s).rung is Rung.R3
    assert resolve_posture("other@bigbank.com", s).rung is Rung.R1


def test_domain_match() -> None:
    s = _settings({"bigbank.com": "R2"})
    assert resolve_posture("anyone@bigbank.com", s).rung is Rung.R2


def test_address_beats_domain() -> None:
    s = _settings({"bigbank.com": "R2", "vip@bigbank.com": "R3"})
    assert resolve_posture("vip@bigbank.com", s).rung is Rung.R3
    assert resolve_posture("staff@bigbank.com", s).rung is Rung.R2


def test_unknown_rung_string_falls_back_to_r1() -> None:
    s = _settings({"bigbank.com": "R9"})
    assert resolve_posture("x@bigbank.com", s).rung is Rung.R1


def test_counterparty_is_case_insensitive() -> None:
    s = _settings({"vip@bigbank.com": "R3", "bigbank.com": "R2"})
    assert resolve_posture("VIP@BigBank.COM", s).rung is Rung.R3
    assert resolve_posture("Staff@BIGBANK.com", s).rung is Rung.R2


def test_empty_string_counterparty_is_r1() -> None:
    assert resolve_posture("", _settings({"acme.com": "R3"})).rung is Rung.R1
