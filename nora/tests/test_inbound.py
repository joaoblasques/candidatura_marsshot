"""U2: inbound webhook — Basic-Auth-before-parse, adapt, replay, rate limit, auth."""

from __future__ import annotations

import base64 as _b64
import json

from fastapi.testclient import TestClient

from app.config import Settings
from app.email.inbound import process_inbound, split_secret_address, verify_basic_auth
from app.email.postmark_inbound import adapt_postmark_inbound
from app.main import create_app
from app.security import RateLimiter, ReplayGuard, resolve_client_ip

from tests.conftest import HASH_LOCAL, WEBHOOK_CRED, basic_auth_header, postmark_payload


def _settings() -> Settings:
    return Settings(
        external_llm_ack=True,
        anthropic_api_key="k",
        provider_api_key="k",
        webhook_basic_auth=WEBHOOK_CRED,
        postmark_sender="nora@example.com",
        ceo_registry={HASH_LOCAL: "ceo@example.com"},
    )


def _raw(**overrides) -> bytes:
    return json.dumps(postmark_payload(**overrides)).encode()


def _fresh_guards() -> tuple[ReplayGuard, RateLimiter]:
    return ReplayGuard(), RateLimiter()


def _process(raw: bytes, settings: Settings, guard, limiter, *, cred=WEBHOOK_CRED):
    encoded = basic_auth_header(cred)["Authorization"]
    return process_inbound(
        raw, encoded, "1.2.3.4", settings, guard, limiter, adapt_postmark_inbound
    )


# --- pure helpers -----------------------------------------------------------


def test_verify_basic_auth_roundtrip() -> None:
    good = basic_auth_header(WEBHOOK_CRED)["Authorization"]
    assert verify_basic_auth(good, WEBHOOK_CRED) is True
    assert verify_basic_auth(good, "other-user:other-pass") is False
    assert verify_basic_auth("Basic not-base64!!", WEBHOOK_CRED) is False
    assert verify_basic_auth("Bearer xyz", WEBHOOK_CRED) is False
    assert verify_basic_auth(None, WEBHOOK_CRED) is False
    assert verify_basic_auth(good, "") is False


def test_split_secret_address_plus_token() -> None:
    assert split_secret_address("abc123hash+corr99@inbound.postmarkapp.com") == (
        "abc123hash",
        "corr99",
    )
    assert split_secret_address("abc123hash@inbound.postmarkapp.com") == ("abc123hash", None)


# --- Postmark adapter -------------------------------------------------------


def test_adapter_maps_core_fields() -> None:
    email = adapt_postmark_inbound(postmark_payload())
    assert email.event_id == "pm-evt-1"
    assert email.secret_address_local == HASH_LOCAL
    assert email.from_address == "ceo@example.com"
    assert email.subject == "Fwd: Proposal"
    assert email.body.startswith("WIYOW")
    assert email.dkim_verified is True


def test_adapter_reads_mailbox_hash_as_correlation_token() -> None:
    email = adapt_postmark_inbound(postmark_payload(MailboxHash="corr99"))
    assert email.correlation_token == "corr99"
    bare = adapt_postmark_inbound(postmark_payload(MailboxHash=""))
    assert bare.correlation_token is None


def test_adapter_dkim_absent_marks_unverified() -> None:
    email = adapt_postmark_inbound(
        postmark_payload(Headers=[{"Name": "X-Spam-Tests", "Value": "SPF_PASS"}])
    )
    assert email.dkim_verified is False


def test_adapter_surfaces_attachment_metadata_only() -> None:
    email = adapt_postmark_inbound(
        postmark_payload(
            Attachments=[
                {"Name": "deck.pdf", "ContentType": "application/pdf", "Content": "BASE64=="}
            ]
        )
    )
    assert len(email.attachments) == 1
    assert email.attachments[0].filename == "deck.pdf"
    assert email.attachments[0].content_type == "application/pdf"


def test_adapter_recipient_falls_back_to_tofull() -> None:
    payload = postmark_payload(ToFull=[{"Email": "abc123hash@inbound.postmarkapp.com"}])
    del payload["OriginalRecipient"]
    assert adapt_postmark_inbound(payload).secret_address_local == HASH_LOCAL


def test_adapter_from_falls_back_to_flat_from() -> None:
    payload = postmark_payload(From="ceo@example.com")
    del payload["FromFull"]
    assert adapt_postmark_inbound(payload).from_address == "ceo@example.com"


def test_adapter_tolerates_hostile_nested_shapes() -> None:
    # Non-dict elements in array fields must not raise (that would become a 500).
    email = adapt_postmark_inbound(
        postmark_payload(Headers=["junk", 42], Attachments=[42, {"NoName": 1}], ToFull="nope")
    )
    assert email.dkim_verified is False
    assert email.attachments == []


# --- process_inbound gauntlet ----------------------------------------------


def test_valid_request_is_accepted() -> None:
    guard, limiter = _fresh_guards()
    outcome = _process(_raw(), _settings(), guard, limiter)
    assert outcome.status_code == 202
    assert outcome.email is not None and outcome.email.event_id == "pm-evt-1"


def test_bad_credential_rejected_before_parse() -> None:
    raw = b"not even json"  # would fail parse, but the credential check comes first
    guard, limiter = _fresh_guards()
    outcome = _process(raw, _settings(), guard, limiter, cred="wrong-user:wrong-pass")
    assert outcome.status_code == 401


def test_replayed_event_id_dropped() -> None:
    guard, limiter = _fresh_guards()
    first = _process(_raw(), _settings(), guard, limiter)
    second = _process(_raw(), _settings(), guard, limiter)
    assert first.status_code == 202
    assert second.status_code == 200  # duplicate dropped


def test_dkim_failure_dropped_unauthorized() -> None:
    guard, limiter = _fresh_guards()
    raw = _raw(Headers=[{"Name": "X-Spam-Tests", "Value": "SPF_PASS"}])
    outcome = _process(raw, _settings(), guard, limiter)
    assert outcome.status_code == 200 and outcome.email is None


def test_from_mismatch_dropped_unauthorized() -> None:
    guard, limiter = _fresh_guards()
    raw = _raw(FromFull={"Email": "attacker@evil.com"}, From="attacker@evil.com")
    outcome = _process(raw, _settings(), guard, limiter)
    assert outcome.status_code == 200 and outcome.email is None


def test_unknown_recipient_dropped() -> None:
    guard, limiter = _fresh_guards()
    raw = _raw(OriginalRecipient="nope-hash@inbound.postmarkapp.com")
    outcome = _process(raw, _settings(), guard, limiter)
    assert outcome.status_code == 200 and outcome.email is None


def test_malformed_payload_after_valid_credential() -> None:
    guard, limiter = _fresh_guards()
    outcome = _process(b"{not json", _settings(), guard, limiter)
    assert outcome.status_code == 400


def test_hostile_nested_shapes_do_not_500() -> None:
    # A structurally hostile (but valid-JSON) payload must degrade, never 500.
    guard, limiter = _fresh_guards()
    raw = _raw(Headers=["junk", 42], Attachments=[1, 2])
    outcome = _process(raw, _settings(), guard, limiter)
    assert outcome.status_code in (200, 400)


def test_adapter_exception_returns_400() -> None:
    # The adapt try/except (incl. AttributeError) degrades to 400, not 500.
    def _boom(_payload: dict):
        raise AttributeError("boom")

    guard, limiter = _fresh_guards()
    encoded = basic_auth_header()["Authorization"]
    outcome = process_inbound(_raw(), encoded, "1.2.3.4", _settings(), guard, limiter, _boom)
    assert outcome.status_code == 400


def test_rate_limit_exceeded() -> None:
    guard = ReplayGuard()
    limiter = RateLimiter(max_requests=1, window_seconds=60)
    first = _process(_raw(), _settings(), guard, limiter)
    second = _process(_raw(MessageID="pm-evt-2"), _settings(), guard, limiter)
    assert first.status_code == 202
    assert second.status_code == 429


# --- #1: client IP behind the tunnel ---------------------------------------


def test_resolve_client_ip_prefers_cf_connecting_ip() -> None:
    assert resolve_client_ip("203.0.113.7", "127.0.0.1") == "203.0.113.7"


def test_resolve_client_ip_falls_back_to_peer_then_unknown() -> None:
    assert resolve_client_ip(None, "10.0.0.4") == "10.0.0.4"
    assert resolve_client_ip("   ", None) == "unknown"
    assert resolve_client_ip(None, None) == "unknown"


def test_route_keys_rate_limit_off_cf_connecting_ip(fake_deps, monkeypatch) -> None:
    captured: list[str] = []
    real = process_inbound

    def _spy(*args, **kwargs):
        captured.append(kwargs.get("client_key", args[2] if len(args) > 2 else None))
        return real(*args, **kwargs)

    monkeypatch.setattr("app.main.process_inbound", _spy)
    client = TestClient(create_app(_settings(), deps=fake_deps))
    client.post(
        "/inbound/webhook",
        content=_raw(),
        headers={**basic_auth_header(), "CF-Connecting-IP": "198.51.100.9"},
    )
    assert captured == ["198.51.100.9"]


# --- #3: RateLimiter table stays bounded under per-IP keying ----------------


def test_rate_limiter_sweeps_stale_buckets_over_capacity() -> None:
    limiter = RateLimiter(max_requests=5, window_seconds=60, max_keys=2)
    limiter.allow("ip:stale-1", now=0.0)
    limiter.allow("ip:stale-2", now=0.0)
    limiter.allow("ip:active", now=950.0)  # 3 keys; sweep triggers on next call
    limiter.allow("ip:trigger", now=951.0)
    assert "ip:stale-1" not in limiter._hits
    assert "ip:stale-2" not in limiter._hits
    assert "ip:active" in limiter._hits  # recent hit within window is kept
    assert "ip:trigger" in limiter._hits


def test_rate_limiter_sweep_does_not_weaken_active_limit() -> None:
    # A sweep must not reset an active source's window: still blocked at the cap.
    limiter = RateLimiter(max_requests=1, window_seconds=60, max_keys=1)
    assert limiter.allow("ip:noisy", now=0.0) is True
    limiter.allow("ip:other", now=0.0)  # over capacity -> sweep on next call
    assert limiter.allow("ip:noisy", now=10.0) is False


# --- real Authentication-Results auth gate (U4) ----------------------------


def _ar_raw(ar_value: str, **overrides) -> bytes:
    return _raw(Headers=[{"Name": "Authentication-Results", "Value": ar_value}], **overrides)


def test_real_dkim_pass_aligned_is_accepted() -> None:
    guard, limiter = _fresh_guards()
    raw = _ar_raw("mx.cloudflare.net; dkim=pass header.d=example.com; spf=pass; dmarc=pass")
    outcome = _process(raw, _settings(), guard, limiter)
    assert outcome.status_code == 202 and outcome.email is not None


def test_spoofed_from_with_unaligned_real_pass_is_dropped() -> None:
    # From forged to the registered address, but the real verdict signs a foreign
    # domain — the alignment check rejects it. This is the core spoof defense.
    guard, limiter = _fresh_guards()
    raw = _ar_raw("mx.cloudflare.net; dkim=pass header.d=attacker.com; dmarc=fail")
    outcome = _process(raw, _settings(), guard, limiter)
    assert outcome.status_code == 200 and outcome.email is None


def test_real_dkim_fail_is_dropped() -> None:
    guard, limiter = _fresh_guards()
    raw = _ar_raw("mx.cloudflare.net; dkim=fail header.d=example.com")
    outcome = _process(raw, _settings(), guard, limiter)
    assert outcome.status_code == 200 and outcome.email is None


def test_missing_verdict_is_dropped_fail_closed() -> None:
    guard, limiter = _fresh_guards()
    raw = _raw(Headers=[])  # no verdict header at all
    outcome = _process(raw, _settings(), guard, limiter)
    assert outcome.status_code == 200 and outcome.email is None


def test_rejected_sender_is_logged(caplog) -> None:
    import logging

    # configure_logging() sets propagate=False on the "app" logger tree; caplog
    # captures via the root handler, so force propagation for the assertion.
    app_logger = logging.getLogger("app")
    previous = app_logger.propagate
    app_logger.propagate = True
    try:
        guard, limiter = _fresh_guards()
        raw = _ar_raw("mx.cloudflare.net; dkim=pass header.d=attacker.com")
        with caplog.at_level(logging.WARNING, logger="app.email.inbound"):
            _process(raw, _settings(), guard, limiter)
    finally:
        app_logger.propagate = previous
    assert any("unauthorized sender" in r.message for r in caplog.records)


def test_multi_address_genuine_forward_accepted_and_routes_to_sender() -> None:
    from app.main import _ceo_address

    settings = Settings(
        external_llm_ack=True,
        anthropic_api_key="k",
        provider_api_key="k",
        webhook_basic_auth=WEBHOOK_CRED,
        postmark_sender="nora@example.com",
        ceo_registry={"democeo": ["f.b@university.example", "f.b@example.net"]},
    )
    guard, limiter = _fresh_guards()
    raw = _ar_raw(
        "mx.cloudflare.net; dkim=pass header.d=university.example; dmarc=pass",
        OriginalRecipient="democeo@nora-bennett.com",
        FromFull={"Email": "f.b@university.example"},
        From="f.b@university.example",
    )
    outcome = _process(raw, settings, guard, limiter)
    assert outcome.status_code == 202 and outcome.email is not None
    # _ceo_address routes the reply to the verified forwarding address, not attacker-steerable
    assert _ceo_address(settings, outcome.email) == "f.b@university.example"


# --- metric events at the inbound seam (U2) --------------------------------


def _metric_events(caplog):
    import json

    events = []
    for r in caplog.records:
        if "metric " in r.message:
            events.append(json.loads(r.message.split("metric ", 1)[1]))
    return [e for e in events if e.get("event") == "metric"]


def _capture_process(raw: bytes, settings: Settings, guard, limiter, caplog, *, cred=WEBHOOK_CRED):
    import logging

    app_logger = logging.getLogger("app")
    previous = app_logger.propagate
    app_logger.propagate = True
    try:
        with caplog.at_level(logging.INFO, logger="app.metrics"):
            outcome = _process(raw, settings, guard, limiter, cred=cred)
    finally:
        app_logger.propagate = previous
    return outcome, _metric_events(caplog)


def test_accepted_emits_inbound_metric_with_ceo_hash(caplog) -> None:
    guard, limiter = _fresh_guards()
    _, events = _capture_process(_raw(), _settings(), guard, limiter, caplog)
    inbound = [e for e in events if e["kind"] == "inbound" and e["outcome"] == "accepted"]
    assert len(inbound) == 1
    assert inbound[0]["ceo_hash"]  # grouping id present, hashed


def test_unauthorized_emits_metric_no_pii(caplog) -> None:
    guard, limiter = _fresh_guards()
    raw = _raw(FromFull={"Email": "attacker@evil.com"}, From="attacker@evil.com")
    _, events = _capture_process(raw, _settings(), guard, limiter, caplog)
    assert any(e["outcome"] == "unauthorized" for e in events)
    assert "attacker@evil.com" not in str(events)  # From never logged


def test_bad_credential_emits_metric(caplog) -> None:
    guard, limiter = _fresh_guards()
    _, events = _capture_process(
        b"x", _settings(), guard, limiter, caplog, cred="wrong-user:wrong-pass"
    )
    assert any(e["outcome"] == "bad_credential" for e in events)


def test_replay_emits_metric(caplog) -> None:
    guard, limiter = _fresh_guards()
    _process(_raw(), _settings(), guard, limiter)  # first, primes the replay guard
    _, events = _capture_process(_raw(), _settings(), guard, limiter, caplog)
    assert any(e["outcome"] == "replay" for e in events)


def test_metric_events_never_contain_subject_or_body(caplog) -> None:
    guard, limiter = _fresh_guards()
    _, events = _capture_process(_raw(), _settings(), guard, limiter, caplog)
    blob = str(events)
    assert "Proposal" not in blob and "WIYOW" not in blob  # subject/body never logged


# --- route wiring -----------------------------------------------------------


def test_webhook_route_accepts_authed_request(fake_deps) -> None:
    client = TestClient(create_app(_settings(), deps=fake_deps))
    resp = client.post("/inbound/webhook", content=_raw(), headers=basic_auth_header())
    assert resp.status_code == 202


def test_route_replay_guard_window_matches_action_ttl(fake_deps) -> None:
    settings = _settings()
    app = create_app(settings, deps=fake_deps)
    assert app.state.replay_guard._window == settings.state_ttl_seconds


def test_replay_caught_past_old_short_window() -> None:
    # A body replayed 700s later (> the former 600s window, < the 24h TTL) must
    # still be rejected, so it can't re-run the pipeline while state is live.
    guard = ReplayGuard(window_seconds=86_400)
    assert guard.is_replay("evt-1", now=0.0) is False
    assert guard.is_replay("evt-1", now=700.0) is True


def test_default_inbound_budget_accommodates_attachments() -> None:
    from app.config import Settings
    s = Settings(external_llm_ack=True, anthropic_api_key="k", webhook_basic_auth="u:p",
                 postmark_sender="n@x.com")
    assert s.max_inbound_bytes >= 15_000_000


def test_adapter_decodes_attachment_content_and_size() -> None:
    raw = _raw(Attachments=[{
        "Name": "plan.txt", "ContentType": "text/plain",
        "ContentLength": 5, "Content": _b64.b64encode(b"hello").decode(),
    }])
    email = adapt_postmark_inbound(json.loads(raw))
    [a] = email.attachments
    assert a.filename == "plan.txt" and a.content == b"hello" and a.size == 5


def test_adapter_omitted_or_bad_content_is_none() -> None:
    raw = _raw(Attachments=[
        {"Name": "big.pdf", "ContentType": "application/pdf", "ContentLength": 99, "Content": ""},
        {"Name": "junk.txt", "ContentLength": 3, "Content": "!!!not-base64!!!"},
    ])
    email = adapt_postmark_inbound(json.loads(raw))
    assert email.attachments[0].content is None and email.attachments[0].size == 99
    assert email.attachments[1].content is None


def test_webhook_route_rejects_bad_credential(fake_deps) -> None:
    client = TestClient(create_app(_settings(), deps=fake_deps))
    resp = client.post(
        "/inbound/webhook",
        content=_raw(),
        headers=basic_auth_header("wrong-user:wrong-pass"),
    )
    assert resp.status_code == 401
