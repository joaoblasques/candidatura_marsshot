"""U1 — metric-event emit helper. Hermetic; asserts no PII reaches the event."""

from __future__ import annotations

import json
import logging

from app.metrics import build_metric_event, emit, hash_id, normalize_subject, thread_key


# --- hash_id ----------------------------------------------------------------

def test_hash_is_stable_for_same_input() -> None:
    assert hash_id("nora", "salt") == hash_id("nora", "salt")


def test_hash_differs_per_identity() -> None:
    assert hash_id("nora", "salt") != hash_id("democeo", "salt")


def test_salt_changes_the_hash() -> None:
    assert hash_id("nora", "salt-a") != hash_id("nora", "salt-b")


def test_hash_is_case_and_whitespace_insensitive() -> None:
    assert hash_id("  Nora ", "s") == hash_id("nora", "s")


def test_empty_salt_still_hashes_not_raw() -> None:
    h = hash_id("nora", "")
    assert h and "nora" not in h


# --- build_metric_event -----------------------------------------------------

def test_event_carries_marker_kind_and_ts() -> None:
    e = build_metric_event("inbound")
    assert e["event"] == "metric"
    assert e["kind"] == "inbound"
    assert isinstance(e["ts"], str) and e["ts"]


def test_ceo_id_becomes_hash_never_raw() -> None:
    e = build_metric_event("inbound", ceo_id="nora", salt="s")
    assert e["ceo_hash"] == hash_id("nora", "s")
    assert "nora" not in json.dumps(e)
    assert "ceo_id" not in e and "secret_address_local" not in e


def test_no_ceo_hash_when_no_ceo_id() -> None:
    assert "ceo_hash" not in build_metric_event("send")


def test_scalar_fields_pass_through() -> None:
    e = build_metric_event("draft", revise=True, revision_count=2, action_code="WIYOW")
    assert e["revise"] is True
    assert e["revision_count"] == 2
    assert e["action_code"] == "WIYOW"


def test_forbidden_keys_are_dropped() -> None:
    e = build_metric_event(
        "inbound",
        subject="Secret deal terms",
        body="confidential",
        from_address="ceo@example.com",
        correlation_token="live-token",
        action_code="S",
    )
    dumped = json.dumps(e)
    for leaked in ("Secret deal terms", "confidential", "ceo@example.com", "live-token"):
        assert leaked not in dumped
    for key in ("subject", "body", "from_address", "correlation_token"):
        assert key not in e
    assert e["action_code"] == "S"  # safe field survives


# --- thread_key / normalize_subject (U4) ------------------------------------

def test_normalize_strips_reply_forward_prefixes() -> None:
    assert normalize_subject("Re: Proposal") == "proposal"
    assert normalize_subject("FWD: Proposal") == "proposal"
    assert normalize_subject("Fw: Proposal") == "proposal"
    assert normalize_subject("Re: Fwd:  Re:  Proposal ") == "proposal"


def test_thread_key_stable_across_cosmetic_variants() -> None:
    k = thread_key("Proposal", "s")
    assert thread_key("Re: Proposal", "s") == k
    assert thread_key("FWD:   proposal", "s") == k
    assert thread_key("  PROPOSAL  ", "s") == k


def test_thread_key_differs_for_different_threads() -> None:
    assert thread_key("Proposal", "s") != thread_key("Invoice", "s")


def test_thread_key_never_contains_raw_subject() -> None:
    assert "proposal" not in thread_key("Secret Proposal", "s").lower()


def test_thread_key_empty_subject_is_stable_sentinel() -> None:
    assert thread_key("", "s") == thread_key("   ", "s")  # no raise, stable


# --- emit -------------------------------------------------------------------

def test_emit_logs_a_parseable_metric_line(caplog) -> None:
    # configure_logging() sets propagate=False on the "app" tree; force it for capture.
    app_logger = logging.getLogger("app")
    previous = app_logger.propagate
    app_logger.propagate = True
    try:
        with caplog.at_level(logging.INFO, logger="app.metrics"):
            emit("inbound", ceo_id="nora", salt="s", action_code="WIYOW", outcome="accepted")
    finally:
        app_logger.propagate = previous

    metric_lines = [r.message for r in caplog.records if "metric " in r.message]
    assert metric_lines, "no metric line emitted"
    payload = json.loads(metric_lines[-1].split("metric ", 1)[1])
    assert payload["event"] == "metric"
    assert payload["kind"] == "inbound"
    assert payload["action_code"] == "WIYOW"
    assert payload["outcome"] == "accepted"
    assert payload["ceo_hash"] == hash_id("nora", "s")


def test_emit_never_raises_on_bad_input() -> None:
    # Non-serializable field must not bubble up (instrumentation is fire-and-forget).
    emit("draft", weird=object())
