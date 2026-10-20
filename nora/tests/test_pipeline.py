"""U7: end-to-end webhook → reply integration (stub drafting + fake sender)."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.state.outbox import FileSendOutbox, InMemorySendOutbox, OutboxRecord

from tests.conftest import (
    FORWARDED_THREAD,
    HASH_LOCAL,
    WEBHOOK_CRED,
    basic_auth_header,
    postmark_payload,
)


def _settings() -> Settings:
    return Settings(
        external_llm_ack=True,
        anthropic_api_key="k",
        provider_api_key="k",
        webhook_basic_auth=WEBHOOK_CRED,
        postmark_sender="nora@example.com",
        ceo_registry={HASH_LOCAL: "ceo@example.com"},
    )


def _post(client: TestClient, **overrides):
    raw = json.dumps(postmark_payload(**overrides)).encode()
    return client.post("/inbound/webhook", content=raw, headers=basic_auth_header())


def test_full_wiyow_flow_sends_draft_to_ceo(fake_deps, fake_provider) -> None:
    client = TestClient(create_app(_settings(), deps=fake_deps))
    resp = _post(client)  # default payload carries WIYOW + forwarded thread
    assert resp.status_code == 202
    # Background task ran; the CEO received exactly one reply, to themselves.
    assert len(fake_provider.sent) == 1
    sent = fake_provider.sent[0]
    assert sent["to"] == "ceo@example.com"
    assert sent["subject"].startswith("Nora · draft reply")
    assert "DRAFTED reply" in sent["body"]
    # Correlation token rides the plus-addressed reply-to on the hash inbox.
    assert sent["reply_to"].startswith(f"{HASH_LOCAL}+")
    assert sent["reply_to"].endswith("@inbound.postmarkapp.com")
    # Domain-DKIM transport (postmark default) sends From the person's domain address.
    assert sent["sender"] == f"{HASH_LOCAL}@inbound.postmarkapp.com"


# --- metric events at the draft/send seam (U3) -----------------------------


def _capture_post(client, caplog, **overrides):
    import logging

    app_logger = logging.getLogger("app")
    prev = app_logger.propagate
    app_logger.propagate = True
    try:
        with caplog.at_level(logging.INFO, logger="app.metrics"):
            resp = _post(client, **overrides)
    finally:
        app_logger.propagate = prev
    events = []
    for r in caplog.records:
        if "metric " in r.message:
            events.append(json.loads(r.message.split("metric ", 1)[1]))
    return resp, [e for e in events if e.get("event") == "metric"]


def test_draft_and_send_metric_events(fake_deps, fake_provider, caplog) -> None:
    client = TestClient(create_app(_settings(), deps=fake_deps))
    _, events = _capture_post(client, caplog)  # default WIYOW
    drafts = [e for e in events if e["kind"] == "draft"]
    sends = [e for e in events if e["kind"] == "send"]
    assert len(drafts) == 1
    assert drafts[0]["revise"] is False
    assert drafts[0]["result_kind"] == "DRAFT"
    assert drafts[0]["action_code"]  # WIYOW resolved to a command code
    assert drafts[0]["nl_fallback"] is False
    assert len(sends) == 1
    assert sends[0]["outcome"] == "sent"
    assert sends[0]["latency_ms"] >= 0
    # No email CONTENT leaks (the command code WIYOW is non-sensitive metadata and
    # legitimately appears as action_code; the subject/body must not).
    assert "Proposal" not in str(events)


def test_nl_request_flags_nl_fallback(fake_deps, caplog) -> None:
    client = TestClient(create_app(_settings(), deps=fake_deps))
    _, events = _capture_post(
        client, caplog, MessageID="pm-nl", TextBody="please write a friendly reply" + FORWARDED_THREAD
    )
    drafts = [e for e in events if e["kind"] == "draft"]
    assert drafts and drafts[0]["nl_fallback"] is True


def test_revise_emits_revise_draft_event(fake_deps, fake_provider, caplog) -> None:
    client = TestClient(create_app(_settings(), deps=fake_deps))
    _post(client)  # initial draft → produces a reply_to carrying the token
    token = fake_provider.sent[0]["reply_to"].split("+", 1)[1].split("@", 1)[0]
    _, events = _capture_post(
        client, caplog, MessageID="pm-rev", MailboxHash=token, TextBody="make it warmer"
    )
    drafts = [e for e in events if e["kind"] == "draft"]
    assert drafts and drafts[0]["revise"] is True
    assert isinstance(drafts[0]["revision_count"], int)


def test_send_failure_emits_failure_metric(fake_deps, caplog, monkeypatch) -> None:
    def _boom(_email):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(fake_deps.pipeline, "handle", _boom)
    client = TestClient(create_app(_settings(), deps=fake_deps))
    _, events = _capture_post(client, caplog)
    sends = [e for e in events if e["kind"] == "send"]
    assert any(s["outcome"] == "failure" for s in sends)


def test_pipeline_failure_alerts_ops_and_still_receipts_ceo(fake_deps, fake_provider, monkeypatch) -> None:
    def _boom(_email):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(fake_deps.pipeline, "handle", _boom)
    settings = Settings(
        external_llm_ack=True,
        anthropic_api_key="k",
        provider_api_key="k",
        webhook_basic_auth=WEBHOOK_CRED,
        postmark_sender="nora@example.com",
        ceo_registry={HASH_LOCAL: "ceo@example.com"},
        alert_email="ops@example.com",
        metrics_salt="s",
    )
    client = TestClient(create_app(settings, deps=fake_deps))
    _post(client)
    recipients = [s["to"] for s in fake_provider.sent]
    assert "ceo@example.com" in recipients  # CEO still gets the failure receipt
    assert "ops@example.com" in recipients  # maintainer gets the ops alert


def test_summarize_flow_sends_summary(fake_deps, fake_provider) -> None:
    client = TestClient(create_app(_settings(), deps=fake_deps))
    resp = _post(client, MessageID="pm-evt-2", TextBody="S" + FORWARDED_THREAD)
    assert resp.status_code == 202
    sent = fake_provider.sent[0]
    assert sent["subject"].startswith("Nora · summary")
    assert sent["reply_to"] is None  # summaries are terminal


def test_background_failure_sends_failure_receipt(fake_provider) -> None:
    # If the pipeline blows up in the background task, the CEO must still get a
    # failure receipt rather than silence (the provider already got 202).
    from app.email.outbound import OutboundSender
    from app.main import Dependencies

    class _BoomPipeline:
        def handle(self, email):  # noqa: ANN001, ANN202
            raise RuntimeError("kaboom")

    deps = Dependencies(pipeline=_BoomPipeline(), outbound=OutboundSender(fake_provider))
    client = TestClient(create_app(_settings(), deps=deps))
    resp = _post(client, MessageID="pm-evt-boom")
    assert resp.status_code == 202
    assert len(fake_provider.sent) == 1
    assert fake_provider.sent[0]["subject"] == "Nora · couldn't process that"
    # Destination resolved from the registry, not the inbound From (KTD6).
    assert fake_provider.sent[0]["to"] == "ceo@example.com"


def test_ceo_address_resolves_from_registry_not_inbound_from() -> None:
    # KTD6: a spoofed From must never steer the destination; the registry wins.
    from app.main import _ceo_address
    from app.models import InboundEmail

    email = InboundEmail(
        event_id="e",
        secret_address_local=HASH_LOCAL,
        from_address="attacker@evil.com",
        dkim_verified=True,
    )
    assert _ceo_address(_settings(), email) == "ceo@example.com"


def test_ceo_address_multi_address_replies_to_forwarding_address() -> None:
    # A person with several allowed From addresses gets the reply at whichever
    # one they forwarded from (verified in the allow-list by authenticate()).
    from app.main import _ceo_address
    from app.models import InboundEmail

    settings = Settings(
        external_llm_ack=True,
        anthropic_api_key="k",
        provider_api_key="k",
        webhook_basic_auth="u:p",
        postmark_sender="nora@example.com",
        ceo_registry={"democeo": ["a@x.com", "b@y.com"]},
    )
    email = InboundEmail(
        event_id="e",
        secret_address_local="democeo",
        from_address="b@y.com",
        dkim_verified=True,
    )
    assert _ceo_address(settings, email) == "b@y.com"


def test_sender_for_uses_person_domain_on_domain_transports() -> None:
    from app.main import _sender_for
    from app.models import InboundEmail

    email = InboundEmail(
        event_id="e", secret_address_local="democeo", from_address="x@y.com", dkim_verified=True
    )
    resend = Settings(
        external_llm_ack=True, anthropic_api_key="k", resend_api_key="r",
        webhook_basic_auth="u:p", postmark_sender="nora@nora-bennett.com",
        transport="resend", address_domain="nora-bennett.com",
        ceo_registry={"democeo": ["x@y.com"]},
    )
    assert _sender_for(resend, email) == "democeo@nora-bennett.com"
    # smtp/dev transport: None so the client's own (Gmail) From is used.
    smtp = Settings(
        external_llm_ack=True, anthropic_api_key="k", webhook_basic_auth="u:p",
        postmark_sender="g@gmail.com", transport="smtp", smtp_host="h",
        address_domain="nora-bennett.com", ceo_registry={"democeo": ["x@y.com"]},
    )
    assert _sender_for(smtp, email) is None


def test_sender_for_applies_per_mailbox_domain_override() -> None:
    # reply-as-client-domain: this mailbox replies From its override domain.
    from app.main import _sender_for
    from app.models import InboundEmail

    email = InboundEmail(
        event_id="e", secret_address_local="nora.bennett",
        from_address="democeo@example.nl", dkim_verified=True,
    )
    settings = Settings(
        external_llm_ack=True, anthropic_api_key="k", resend_api_key="r",
        webhook_basic_auth="u:p", postmark_sender="nora@nora-bennett.com",
        transport="resend", address_domain="nora-bennett.com",
        ceo_registry={"nora.bennett": ["democeo@example.nl"]},
        sender_domain_overrides={"nora.bennett": "example.nl"},
    )
    assert _sender_for(settings, email) == "nora.bennett@example.nl"


def test_sender_for_override_leaves_other_mailboxes_on_address_domain() -> None:
    from app.main import _sender_for
    from app.models import InboundEmail

    email = InboundEmail(
        event_id="e", secret_address_local="democeo",
        from_address="x@y.com", dkim_verified=True,
    )
    settings = Settings(
        external_llm_ack=True, anthropic_api_key="k", resend_api_key="r",
        webhook_basic_auth="u:p", postmark_sender="nora@nora-bennett.com",
        transport="resend", address_domain="nora-bennett.com",
        ceo_registry={"democeo": ["x@y.com"]},
        sender_domain_overrides={"nora.bennett": "example.nl"},
    )
    assert _sender_for(settings, email) == "democeo@nora-bennett.com"


def test_sender_domain_overrides_parses_json_env_string() -> None:
    settings = Settings(
        external_llm_ack=True, anthropic_api_key="k", webhook_basic_auth="u:p",
        postmark_sender="nora@nora-bennett.com",
        sender_domain_overrides='{"nora.bennett": "example.nl"}',
    )
    assert settings.sender_domain_overrides == {"nora.bennett": "example.nl"}


def test_oversized_inbound_body_is_rejected_before_processing(fake_deps, fake_provider) -> None:
    settings = Settings(
        external_llm_ack=True,
        anthropic_api_key="k",
        provider_api_key="k",
        webhook_basic_auth=WEBHOOK_CRED,
        postmark_sender="nora@example.com",
        ceo_registry={HASH_LOCAL: "ceo@example.com"},
        max_inbound_bytes=100,
    )
    client = TestClient(create_app(settings, deps=fake_deps))
    resp = client.post(
        "/inbound/webhook", content=b"x" * 200, headers=basic_auth_header()
    )
    assert resp.status_code == 413  # Payload Too Large
    assert fake_provider.sent == []  # rejected before any parsing/drafting


# --- consent-gated meeting proposal: propose → confirm → one send (U6) ---------


def _propose(client, fake_provider) -> str:
    """Run an SM propose and return the pending-action token from the CEO receipt."""
    resp = _post(client, MessageID="pm-sm", TextBody="SM" + FORWARDED_THREAD)
    assert resp.status_code == 202
    proposal = fake_provider.sent[-1]
    assert proposal["to"] == "ceo@example.com"
    assert proposal["subject"].startswith("Nora · proposed meeting email")
    # Nothing was sent to the third party at propose time (R5/R6).
    assert all(s["to"] != "sarah@acme.com" for s in fake_provider.sent)
    return proposal["reply_to"].split("+", 1)[1].split("@", 1)[0]


def test_confirm_executes_single_third_party_send_from_assistant_address(
    fake_deps, fake_provider
) -> None:
    client = TestClient(create_app(_settings(), deps=fake_deps))
    token = _propose(client, fake_provider)
    before = len(fake_provider.sent)

    resp = _post(client, MessageID="pm-ag", MailboxHash=token, TextBody="AG")
    assert resp.status_code == 202
    new = fake_provider.sent[before:]

    third_party = [s for s in new if s["to"] == "sarah@acme.com"]
    assert len(third_party) == 1  # exactly one real send (AE2)
    tp = third_party[0]
    # From is Nora's labeled brand address (assistant_address empty → postmark_sender),
    # NEVER the CEO's secret per-person address (R8/KTD7).
    assert tp["sender"] == "nora@example.com"
    assert tp["sender"] != f"{HASH_LOCAL}@inbound.postmarkapp.com"
    assert tp["reply_to"] == "ceo@example.com"  # the party's reply routes to the CEO
    assert tp["idempotency_key"] == token  # keyed on the single-use token (KTD-H)

    # The CEO gets a "sent" receipt — only after the send — naming the recipient.
    receipt = [s for s in new if s["to"] == "ceo@example.com"]
    assert len(receipt) == 1
    assert receipt[0]["subject"].startswith("Nora · sent to sarah@acme.com")
    assert "sarah@acme.com" in receipt[0]["body"]


def test_duplicate_confirm_never_double_sends(fake_deps, fake_provider) -> None:
    client = TestClient(create_app(_settings(), deps=fake_deps))
    token = _propose(client, fake_provider)
    _post(client, MessageID="pm-ag1", MailboxHash=token, TextBody="AG")
    _post(client, MessageID="pm-ag2", MailboxHash=token, TextBody="AG")  # replay
    third_party = [s for s in fake_provider.sent if s["to"] == "sarah@acme.com"]
    assert len(third_party) == 1  # still exactly one total (AE4 / R6)


def test_decline_sends_nothing_to_third_party(fake_deps, fake_provider) -> None:
    client = TestClient(create_app(_settings(), deps=fake_deps))
    token = _propose(client, fake_provider)
    _post(client, MessageID="pm-ang", MailboxHash=token, TextBody="ANG")
    assert all(s["to"] != "sarah@acme.com" for s in fake_provider.sent)
    cancels = [
        s for s in fake_provider.sent
        if s["to"] == "ceo@example.com" and "cancel" in s["subject"].lower()
    ]
    assert cancels  # CEO gets a cancellation receipt


def test_confirm_send_failure_yields_single_accurate_receipt(
    fake_deps, fake_provider, monkeypatch, caplog
) -> None:
    import logging

    client = TestClient(create_app(_settings(), deps=fake_deps))
    token = _propose(client, fake_provider)

    def _boom(*args, **kwargs):
        raise RuntimeError("provider down")

    monkeypatch.setattr(fake_deps.outbound, "send_on_behalf", _boom)
    before = len(fake_provider.sent)

    app_logger = logging.getLogger("app")
    prev = app_logger.propagate
    app_logger.propagate = True
    try:
        with caplog.at_level(logging.ERROR, logger="app.main"):
            _post(client, MessageID="pm-ag", MailboxHash=token, TextBody="AG")
    finally:
        app_logger.propagate = prev

    new = fake_provider.sent[before:]
    # No third-party send recorded, and no false "sent" receipt preceding the failure.
    assert all(s["to"] != "sarah@acme.com" for s in new)
    assert [s for s in new if s["subject"].startswith("Nora · sent to")] == []
    # Exactly one failure receipt, naming the recipient (CEO situational awareness).
    fails = [s for s in new if s["subject"] == "Nora · couldn't send to sarah@acme.com"]
    assert len(fails) == 1
    assert "sarah@acme.com" in fails[0]["body"]
    # The recipient address is redacted in logs (it only belongs in the CEO receipt).
    assert "sarah@acme.com" not in caplog.text


# --- redacted action-loop metrics (U7) -----------------------------------------


def test_action_metrics_proposed_confirmed_sent_and_redacted(
    fake_deps, fake_provider, caplog
) -> None:
    client = TestClient(create_app(_settings(), deps=fake_deps))
    _, ev_propose = _capture_post(client, caplog, MessageID="pm-sm", TextBody="SM" + FORWARDED_THREAD)
    proposed = [e for e in ev_propose if e["kind"] == "action"]
    assert [e["phase"] for e in proposed] == ["proposed"]

    token = fake_provider.sent[-1]["reply_to"].split("+", 1)[1].split("@", 1)[0]
    caplog.clear()
    _, ev_confirm = _capture_post(client, caplog, MessageID="pm-ag", MailboxHash=token, TextBody="AG")
    confirm_actions = [e for e in ev_confirm if e["kind"] == "action"]
    # Pipeline emits 'confirmed', then main emits 'sent' after the send returns.
    assert [e["phase"] for e in confirm_actions] == ["confirmed", "sent"]

    # Every action event carries the salted ceo_hash and NO recipient/content.
    for e in proposed + confirm_actions:
        assert "ceo_hash" in e
        assert all(k not in e for k in ("recipient", "subject", "body", "text"))
        assert "sarah@acme.com" not in json.dumps(e, default=str)


def test_action_metric_declined(fake_deps, fake_provider, caplog) -> None:
    client = TestClient(create_app(_settings(), deps=fake_deps))
    _post(client, MessageID="pm-sm", TextBody="SM" + FORWARDED_THREAD)
    token = fake_provider.sent[-1]["reply_to"].split("+", 1)[1].split("@", 1)[0]
    _, ev = _capture_post(client, caplog, MessageID="pm-ang", MailboxHash=token, TextBody="ANG")
    assert [e["phase"] for e in ev if e["kind"] == "action"] == ["declined"]


def test_unauthorized_sender_produces_no_reply(fake_deps, fake_provider) -> None:
    client = TestClient(create_app(_settings(), deps=fake_deps))
    resp = _post(
        client,
        MessageID="pm-evt-3",
        FromFull={"Email": "attacker@evil.com"},
        From="attacker@evil.com",
    )
    assert resp.status_code == 200  # dropped
    assert fake_provider.sent == []  # nothing drafted or sent


# --- durable outbox: write-before-send + clear-on-both-outcomes (T5) -----------


class _OutboxSpyProvider:
    """Records, at each send, which outbox tokens are still pending — proving the
    record was written BEFORE the third-party send."""

    def __init__(self, outbox) -> None:
        self.outbox = outbox
        self.pending_per_send: list[list[str]] = []
        self.sent: list[dict] = []

    def send(self, *, to, subject, body, reply_to, sender=None, idempotency_key=None, attachments=None) -> None:
        self.pending_per_send.append([r.token for r in self.outbox.pending()])
        self.sent.append({"to": to, "sender": sender, "reply_to": reply_to,
                          "idempotency_key": idempotency_key, "subject": subject})


def _deps_with_outbox(outbox, provider):
    from app.email.outbound import OutboundSender
    from app.main import Dependencies
    from app.pipeline import Pipeline
    from app.state.store import InMemoryStateStore
    from tests.conftest import StubDrafting
    s = _settings()
    pipe = Pipeline(settings=s, drafting=StubDrafting(), store=InMemoryStateStore(s.state_ttl_seconds))
    return Dependencies(pipeline=pipe, outbound=OutboundSender(provider), outbox=outbox)


def test_confirm_writes_outbox_before_send_then_clears(fake_deps, fake_provider) -> None:
    box = InMemorySendOutbox()
    provider = _OutboxSpyProvider(box)
    deps = _deps_with_outbox(box, provider)
    client = TestClient(create_app(_settings(), deps=deps))
    _post(client, MessageID="pm-sm", TextBody="SM" + FORWARDED_THREAD)
    token = provider.sent[-1]["reply_to"].split("+", 1)[1].split("@", 1)[0]
    _post(client, MessageID="pm-ag", MailboxHash=token, TextBody="AG")
    tp_index = next(i for i, s in enumerate(provider.sent) if s["to"] == "sarah@acme.com")
    assert provider.pending_per_send[tp_index]  # non-empty: a record existed at send
    assert box.pending() == []  # removed after the send completed


def test_caught_send_failure_clears_outbox_no_replay(fake_provider, monkeypatch) -> None:
    box = InMemorySendOutbox()
    deps = _deps_with_outbox(box, fake_provider)
    client = TestClient(create_app(_settings(), deps=deps))
    _post(client, MessageID="pm-sm", TextBody="SM" + FORWARDED_THREAD)
    token = fake_provider.sent[-1]["reply_to"].split("+", 1)[1].split("@", 1)[0]

    def _boom(*a, **k):
        raise RuntimeError("provider down")

    monkeypatch.setattr(deps.outbound, "send_on_behalf", _boom)
    _post(client, MessageID="pm-ag", MailboxHash=token, TextBody="AG")
    assert box.pending() == []  # removed even on caught failure — no replay


def test_boot_replay_completes_stranded_send(fake_provider, tmp_path) -> None:
    # Simulate a crash after consume-before-send: a record is on disk, nothing sent.
    fake_provider.supports_idempotency = True  # prod uses Resend → safe to replay
    path = str(tmp_path / "outbox.json")
    box = FileSendOutbox(path)
    box.record(OutboxRecord(
        token="corr-strand", recipient="sarah@acme.com",
        subject="Meeting request: Proposal", body="Hi Sarah ...",
        ceo_address="ceo@example.com", ceo_sender=None,
        ceo_id="abc123hash", event_id="evt-strand",
    ))
    deps = _deps_with_outbox(FileSendOutbox(path), fake_provider)
    # Boot: create_app runs _replay_outbox before serving.
    create_app(_settings(), deps=deps)
    third_party = [s for s in fake_provider.sent if s["to"] == "sarah@acme.com"]
    assert len(third_party) == 1                               # completed exactly once
    assert third_party[0]["idempotency_key"] == "corr-strand"  # idempotency-keyed
    assert third_party[0]["reply_to"] == "ceo@example.com"
    ceo = [s for s in fake_provider.sent if s["to"] == "ceo@example.com"]
    assert any(s["subject"].startswith("Nora · sent to sarah@acme.com") for s in ceo)
    assert FileSendOutbox(path).pending() == []               # cleared after replay


def test_outbox_write_failure_does_not_block_send(fake_provider) -> None:
    # Durability is best-effort: if the outbox write fails, the real confirmed send
    # must still go out (never block a send on persistence).
    class _RaisingOutbox(InMemorySendOutbox):
        def record(self, entry) -> None:  # noqa: ANN001
            raise RuntimeError("disk full")

    deps = _deps_with_outbox(_RaisingOutbox(), fake_provider)
    client = TestClient(create_app(_settings(), deps=deps))
    _post(client, MessageID="pm-sm", TextBody="SM" + FORWARDED_THREAD)
    token = fake_provider.sent[-1]["reply_to"].split("+", 1)[1].split("@", 1)[0]
    _post(client, MessageID="pm-ag", MailboxHash=token, TextBody="AG")
    third_party = [s for s in fake_provider.sent if s["to"] == "sarah@acme.com"]
    assert len(third_party) == 1  # send still happened despite the failed outbox write
    assert any(
        s["to"] == "ceo@example.com" and s["subject"].startswith("Nora · sent to")
        for s in fake_provider.sent
    )


def test_proposal_survives_restart_via_file_store(fake_provider, tmp_path) -> None:
    # propose with a FILE-backed pending-action store, then build a NEW pipeline at the
    # same path (simulated restart) and confirm with AG — the proposal must still match.
    from app.email.outbound import OutboundSender
    from app.main import Dependencies
    from app.pipeline import Pipeline
    from app.state.store import FilePendingActionStore, InMemoryStateStore
    from tests.conftest import StubDrafting

    s = _settings()
    pa_path = str(tmp_path / "pa.json")

    def _deps():
        pipe = Pipeline(
            settings=s, drafting=StubDrafting(),
            store=InMemoryStateStore(s.state_ttl_seconds),
            actions=FilePendingActionStore(pa_path, s.state_ttl_seconds),
        )
        return Dependencies(pipeline=pipe, outbound=OutboundSender(fake_provider))

    client1 = TestClient(create_app(s, deps=_deps()))
    _post(client1, MessageID="pm-sm", TextBody="SM" + FORWARDED_THREAD)
    token = fake_provider.sent[-1]["reply_to"].split("+", 1)[1].split("@", 1)[0]

    client2 = TestClient(create_app(s, deps=_deps()))  # fresh pipeline at same store path = restart
    before = len(fake_provider.sent)
    _post(client2, MessageID="pm-ag", MailboxHash=token, TextBody="AG")
    new = fake_provider.sent[before:]
    assert any(s_["to"] == "sarah@acme.com" for s_ in new), "AG after restart did not send"


def test_boot_replay_send_failure_receipts_and_clears(fake_provider, tmp_path, monkeypatch) -> None:
    # A stranded record whose re-send fails at boot → one accurate failure receipt to
    # the CEO + the record cleared (never re-stranded), no third-party send recorded.
    fake_provider.supports_idempotency = True  # idempotent transport → replay attempted
    path = str(tmp_path / "outbox.json")
    FileSendOutbox(path).record(OutboxRecord(
        token="corr-strand2", recipient="sarah@acme.com",
        subject="Meeting request: Proposal", body="Hi Sarah ...",
        ceo_address="ceo@example.com", ceo_sender=None,
        ceo_id="abc123hash", event_id="evt-strand2",
    ))
    deps = _deps_with_outbox(FileSendOutbox(path), fake_provider)

    def _boom(*a, **k):
        raise RuntimeError("provider down")

    monkeypatch.setattr(deps.outbound, "send_on_behalf", _boom)
    create_app(_settings(), deps=deps)  # boot replay attempts + fails the send
    assert all(s["to"] != "sarah@acme.com" for s in fake_provider.sent)
    fails = [s for s in fake_provider.sent if s["subject"] == "Nora · couldn't send to sarah@acme.com"]
    assert len(fails) == 1
    assert FileSendOutbox(path).pending() == []  # cleared, not re-stranded


def test_higher_assurance_relationship_is_declined_without_drafting(fake_provider) -> None:
    # A counterparty marked R3 must be declined BEFORE any drafting (no LLM call).
    from app.email.outbound import OutboundSender
    from app.main import Dependencies
    from app.pipeline import Pipeline
    from app.state.store import InMemoryStateStore

    class _StubDraftingBoom:
        def produce(self, req, intent):
            raise AssertionError("drafting must not be called for a declined relationship")

    s = Settings(
        external_llm_ack=True, anthropic_api_key="k", provider_api_key="k",
        webhook_basic_auth=WEBHOOK_CRED, postmark_sender="nora@example.com",
        ceo_registry={HASH_LOCAL: "ceo@example.com"},
        posture_map={"acme.com": "R3"},  # FORWARDED_THREAD counterparty is sarah@acme.com
    )
    deps = Dependencies(
        pipeline=Pipeline(settings=s, drafting=_StubDraftingBoom(), store=InMemoryStateStore(s.state_ttl_seconds)),
        outbound=OutboundSender(fake_provider),
    )
    client = TestClient(create_app(s, deps=deps))
    resp = _post(client, MessageID="pm-r3", TextBody="RE" + FORWARDED_THREAD)
    assert resp.status_code == 202
    sent = fake_provider.sent[-1]
    assert sent["to"] == "ceo@example.com"
    assert ("higher-assurance" in sent["body"].lower()) or ("haven't processed" in sent["body"].lower())


def test_unmapped_relationship_drafts_as_normal(fake_deps, fake_provider) -> None:
    # Default posture (empty map) -> R1 -> normal draft (regression).
    client = TestClient(create_app(_settings(), deps=fake_deps))
    _post(client)  # default WIYOW forward, counterparty sarah@acme.com, no posture_map
    assert "DRAFTED reply" in fake_provider.sent[-1]["body"]


def test_consumed_replay_guard_survives_restart_no_double_send(fake_provider, tmp_path) -> None:
    # Trust-critical: the no-double-send guarantee must hold ACROSS restarts. Once a
    # proposal is confirmed (consumed) and persisted, a second AG after another restart
    # is a no-op — never a second third-party send.
    from app.email.outbound import OutboundSender
    from app.main import Dependencies
    from app.pipeline import Pipeline
    from app.state.store import FilePendingActionStore, InMemoryStateStore
    from tests.conftest import StubDrafting

    s = _settings()
    pa_path = str(tmp_path / "pa.json")

    def _deps():
        pipe = Pipeline(
            settings=s, drafting=StubDrafting(),
            store=InMemoryStateStore(s.state_ttl_seconds),
            actions=FilePendingActionStore(pa_path, s.state_ttl_seconds),
        )
        return Dependencies(pipeline=pipe, outbound=OutboundSender(fake_provider))

    _post(TestClient(create_app(s, deps=_deps())), MessageID="pm-sm", TextBody="SM" + FORWARDED_THREAD)
    token = fake_provider.sent[-1]["reply_to"].split("+", 1)[1].split("@", 1)[0]
    # First AG (restart) → confirms + sends + consumes (persisted to the file store).
    _post(TestClient(create_app(s, deps=_deps())), MessageID="pm-ag1", MailboxHash=token, TextBody="AG")
    # Second AG (another restart) → consumed record seen on disk → no second send.
    _post(TestClient(create_app(s, deps=_deps())), MessageID="pm-ag2", MailboxHash=token, TextBody="AG")
    third_party = [s_ for s_ in fake_provider.sent if s_["to"] == "sarah@acme.com"]
    assert len(third_party) == 1, "consumed-replay guard failed across restart — double send"


# --- consent-gated calendar hold: SM.A propose → AG → create_event (T4/T5) ----


class _FakeCalendarClient:
    """Records create_event calls instead of hitting a real backend. ``busy`` returns a
    configurable conflict list (propose-time conflict check)."""

    def __init__(self, busy=None) -> None:
        from app.calendar import CalendarEvent
        self.created: list[CalendarEvent] = []
        self.invites: list[bool] = []
        self._busy = list(busy or [])
        self.busy_calls: list[tuple[str, str]] = []

    def create_event(self, event, *, invite: bool = False) -> str:
        self.created.append(event)
        self.invites.append(invite)
        return "fake-event-id"

    def busy(self, start: str, end: str) -> list[str]:
        self.busy_calls.append((start, end))
        return list(self._busy)


def _propose_calendar(client, fake_provider) -> str:
    """Run an SM.A propose and return the pending-action token from the CEO receipt."""
    resp = _post(client, MessageID="pm-sma", TextBody="SM.A" + FORWARDED_THREAD)
    assert resp.status_code == 202
    proposal = fake_provider.sent[-1]
    assert proposal["to"] == "ceo@example.com"
    # Calendar-specific proposal receipt (not the email-proposal copy).
    assert proposal["subject"].startswith("Nora · proposed calendar hold")
    # Nothing sent to the third party yet.
    assert all(s["to"] != "sarah@acme.com" for s in fake_provider.sent)
    # Receipt mentions "time to be confirmed" so CEO knows it's a placeholder hold.
    assert "to be confirmed" in proposal["body"]
    return proposal["reply_to"].split("+", 1)[1].split("@", 1)[0]


def test_sm_a_propose_calendar_stores_pending_hold_no_event_created(
    fake_deps, fake_provider
) -> None:
    """SM.A → CEO gets PROPOSAL receipt mentioning 'to be confirmed'; a pending
    calendar_hold record is stored; the (Null) calendar is NOT touched."""
    fake_cal = _FakeCalendarClient()
    fake_deps.calendar = fake_cal
    client = TestClient(create_app(_settings(), deps=fake_deps))

    token = _propose_calendar(client, fake_provider)

    # Pending record is stored under the token.
    record = fake_deps.pipeline.actions.get(token)
    assert record is not None
    assert record.action_kind == "calendar_hold"
    assert "summary" in record.payload
    assert record.recipient == "sarah@acme.com"

    # Calendar was NOT touched yet — the hold only executes after AG.
    assert fake_cal.created == []


def test_sm_a_propose_flags_calendar_conflict(fake_deps, fake_provider) -> None:
    # A concrete time that clashes with an existing event → the proposal carries a heads-up.
    fake_deps.pipeline.calendar = _FakeCalendarClient(busy=["Board call"])
    client = TestClient(create_app(_settings(), deps=fake_deps))
    _post(client, MessageID="pm-sma-conf",
          TextBody="SM.A [meet 2026-07-01 15:00]" + FORWARDED_THREAD)
    body = fake_provider.sent[-1]["body"]
    assert "Heads up" in body and "Board call" in body
    assert fake_deps.pipeline.calendar.busy_calls  # the proposed window was checked


def test_sm_a_propose_no_heads_up_when_calendar_clear(fake_deps, fake_provider) -> None:
    fake_deps.pipeline.calendar = _FakeCalendarClient(busy=[])
    client = TestClient(create_app(_settings(), deps=fake_deps))
    _post(client, MessageID="pm-sma-clear",
          TextBody="SM.A [meet 2026-07-01 15:00]" + FORWARDED_THREAD)
    assert "Heads up" not in fake_provider.sent[-1]["body"]


def test_sm_a_propose_conflict_check_is_failsafe(fake_deps, fake_provider) -> None:
    class _BoomCal(_FakeCalendarClient):
        def busy(self, start, end):
            raise RuntimeError("calendar read down")

    fake_deps.pipeline.calendar = _BoomCal()
    client = TestClient(create_app(_settings(), deps=fake_deps))
    resp = _post(client, MessageID="pm-sma-boom",
                 TextBody="SM.A [meet 2026-07-01 15:00]" + FORWARDED_THREAD)
    assert resp.status_code == 202  # the proposal is still produced despite the read error
    proposal = fake_provider.sent[-1]
    assert proposal["subject"].startswith("Nora · proposed calendar hold")
    assert "Heads up" not in proposal["body"]


def test_sm_a_no_conflict_check_for_placeholder_time(fake_deps, fake_provider) -> None:
    # No concrete time → no busy() read at all (nothing to check).
    fake_deps.pipeline.calendar = _FakeCalendarClient(busy=["Board call"])
    client = TestClient(create_app(_settings(), deps=fake_deps))
    _post(client, MessageID="pm-sma-noplace", TextBody="SM.A" + FORWARDED_THREAD)
    assert fake_deps.pipeline.calendar.busy_calls == []
    assert "Heads up" not in fake_provider.sent[-1]["body"]


def test_sm_a_ag_creates_calendar_event_exactly_once(
    fake_provider
) -> None:
    """SM.A propose → AG → fake CalendarClient.create_event called exactly once;
    CEO gets a 'Held' receipt. Duplicate AG is a no-op (replay-safe)."""
    from app.email.outbound import OutboundSender
    from app.main import Dependencies
    from app.pipeline import Pipeline
    from app.state.store import InMemoryStateStore
    from tests.conftest import StubDrafting

    s = _settings()
    fake_cal = _FakeCalendarClient()
    pipe = Pipeline(settings=s, drafting=StubDrafting(), store=InMemoryStateStore(s.state_ttl_seconds))
    deps = Dependencies(
        pipeline=pipe,
        outbound=OutboundSender(fake_provider),
        calendar=fake_cal,
    )
    client = TestClient(create_app(s, deps=deps))

    token = _propose_calendar(client, fake_provider)
    before = len(fake_provider.sent)

    # First AG → should create the hold.
    resp = _post(client, MessageID="pm-sma-ag1", MailboxHash=token, TextBody="AG")
    assert resp.status_code == 202
    assert len(fake_cal.created) == 1
    created = fake_cal.created[0]
    assert "Proposal" in created.summary or "Meeting" in created.summary
    assert "sarah@acme.com" in created.attendees

    # CEO gets a "Held" receipt.
    new = fake_provider.sent[before:]
    ceo_receipts = [s for s in new if s["to"] == "ceo@example.com"]
    assert ceo_receipts
    assert any("held" in r["body"].lower() or "Held" in r["body"] for r in ceo_receipts)

    # Duplicate AG — record consumed, no second create_event.
    _post(client, MessageID="pm-sma-ag2", MailboxHash=token, TextBody="AG")
    assert len(fake_cal.created) == 1  # still exactly one


def test_sm_a_ag_with_null_calendar_yields_unavailable_receipt(
    fake_deps, fake_provider
) -> None:
    """SM.A → AG with the default NullCalendarClient → CEO gets 'calendar not
    available' receipt; no crash; no third-party email sent."""
    # fake_deps uses NullCalendarClient by default (Dependencies.calendar default).
    client = TestClient(create_app(_settings(), deps=fake_deps))

    token = _propose_calendar(client, fake_provider)
    before = len(fake_provider.sent)

    resp = _post(client, MessageID="pm-sma-null", MailboxHash=token, TextBody="AG")
    assert resp.status_code == 202

    new = fake_provider.sent[before:]
    # No third-party email sent.
    assert all(s["to"] != "sarah@acme.com" for s in new)
    # CEO gets the graceful "not available" receipt.
    unavail = [s for s in new if "calendar not available" in s["subject"].lower()]
    assert unavail, f"Expected 'calendar not available' subject, got: {[s['subject'] for s in new]}"
    assert "nothing was scheduled" in unavail[0]["body"].lower()


def test_sm_without_a_modifier_still_sends_email_proposal(
    fake_deps, fake_provider
) -> None:
    """Regression: plain SM (no .A) still routes to the email-proposal path, not calendar."""
    client = TestClient(create_app(_settings(), deps=fake_deps))
    resp = _post(client, MessageID="pm-sm-reg", TextBody="SM" + FORWARDED_THREAD)
    assert resp.status_code == 202
    proposal = fake_provider.sent[-1]
    assert proposal["to"] == "ceo@example.com"
    assert proposal["subject"].startswith("Nora · proposed meeting email")
    # Pending record is a send_email action, not a calendar_hold.
    token = proposal["reply_to"].split("+", 1)[1].split("@", 1)[0]
    record = fake_deps.pipeline.actions.get(token)
    assert record is not None
    assert record.action_kind == "send_email"


# --- FI/I research path (default-off NullResearchClient) -----------------------


class _FakeResearch:
    def __init__(self, out="Findings: Sarah Lee is VP Eng at Acme."):
        self.out = out
        self.queries = []

    def research(self, query: str) -> str:
        self.queries.append(query)
        return self.out


def test_fi_with_research_backend_returns_findings(fake_provider) -> None:
    from app.email.outbound import OutboundSender
    from app.main import Dependencies
    from app.pipeline import Pipeline
    from app.state.store import InMemoryStateStore
    from tests.conftest import StubDrafting

    s = _settings()
    pipe = Pipeline(settings=s, drafting=StubDrafting(),
                    store=InMemoryStateStore(s.state_ttl_seconds), research=_FakeResearch())
    deps = Dependencies(pipeline=pipe, outbound=OutboundSender(fake_provider))
    client = TestClient(create_app(s, deps=deps))
    _post(client, MessageID="pm-fi", TextBody="FI" + FORWARDED_THREAD)
    assert "Findings:" in fake_provider.sent[-1]["body"]


def test_fi_without_backend_declines_gracefully(fake_deps, fake_provider) -> None:
    client = TestClient(create_app(_settings(), deps=fake_deps))
    _post(client, MessageID="pm-fi2", TextBody="FI" + FORWARDED_THREAD)
    body = fake_provider.sent[-1]["body"].lower()
    assert "research" in body or "not available" in body or "isn't available" in body


# --- R2 in-tenant draft seam ---------------------------------------------------


class _FakeTenant:
    def __init__(self): self.saved = []  # noqa: E704
    def save_draft(self, to, subject, body) -> str:
        self.saved.append({"to": to, "subject": subject, "body": body})
        return "draft-1"


def test_r2_relationship_saved_to_tenant_when_configured(fake_provider) -> None:
    from app.email.outbound import OutboundSender
    from app.main import Dependencies
    from app.pipeline import Pipeline
    from app.state.store import InMemoryStateStore
    from tests.conftest import StubDrafting
    s = Settings(external_llm_ack=True, anthropic_api_key="k", provider_api_key="k",
                 webhook_basic_auth=WEBHOOK_CRED, postmark_sender="nora@example.com",
                 ceo_registry={HASH_LOCAL: "ceo@example.com"}, posture_map={"acme.com": "R2"})
    fake_tenant = _FakeTenant()
    pipe = Pipeline(settings=s, drafting=StubDrafting(),
                    store=InMemoryStateStore(s.state_ttl_seconds), tenant=fake_tenant)
    deps = Dependencies(pipeline=pipe, outbound=OutboundSender(fake_provider))
    client = TestClient(create_app(s, deps=deps))
    _post(client, MessageID="pm-r2", TextBody="RE" + FORWARDED_THREAD)
    assert len(fake_tenant.saved) == 1
    assert "DRAFTED reply" in fake_tenant.saved[0]["body"]
    assert "tenant" in fake_provider.sent[-1]["body"].lower()


def test_r2_relationship_declines_when_no_tenant_backend(fake_provider) -> None:
    from app.email.outbound import OutboundSender
    from app.main import Dependencies
    from app.pipeline import Pipeline
    from app.state.store import InMemoryStateStore
    from tests.conftest import StubDrafting
    s = Settings(external_llm_ack=True, anthropic_api_key="k", provider_api_key="k",
                 webhook_basic_auth=WEBHOOK_CRED, postmark_sender="nora@example.com",
                 ceo_registry={HASH_LOCAL: "ceo@example.com"}, posture_map={"acme.com": "R2"})
    # NullTenantDraftClient is the default — R2 must decline without any drafting call.
    pipe = Pipeline(settings=s, drafting=StubDrafting(), store=InMemoryStateStore(s.state_ttl_seconds))
    deps = Dependencies(pipeline=pipe, outbound=OutboundSender(fake_provider))
    client = TestClient(create_app(s, deps=deps))
    _post(client, MessageID="pm-r2b", TextBody="RE" + FORWARDED_THREAD)
    body = fake_provider.sent[-1]["body"].lower()
    assert "higher-assurance" in body or "haven't processed" in body


def test_r2_null_tenant_never_invokes_the_llm(fake_provider) -> None:
    # Data-trust guarantee: a default-R2 relationship (no tenant backend) must decline
    # WITHOUT ever calling the drafting service — R2 content never hits the (non-DPA) LLM.
    from app.email.outbound import OutboundSender
    from app.main import Dependencies
    from app.pipeline import Pipeline
    from app.state.store import InMemoryStateStore

    class _BoomDrafting:
        def produce(self, req, intent):
            raise AssertionError("drafting must NOT be called for an R2 relationship with no tenant backend")

    s = Settings(
        external_llm_ack=True, anthropic_api_key="k", provider_api_key="k",
        webhook_basic_auth=WEBHOOK_CRED, postmark_sender="nora@example.com",
        ceo_registry={HASH_LOCAL: "ceo@example.com"}, posture_map={"acme.com": "R2"},
    )
    deps = Dependencies(
        pipeline=Pipeline(settings=s, drafting=_BoomDrafting(), store=InMemoryStateStore(s.state_ttl_seconds)),
        outbound=OutboundSender(fake_provider),
    )
    client = TestClient(create_app(s, deps=deps))
    resp = _post(client, MessageID="pm-r2boom", TextBody="RE" + FORWARDED_THREAD)
    assert resp.status_code == 202
    body = fake_provider.sent[-1]["body"].lower()
    assert "higher-assurance" in body or "haven't processed" in body


# --- R3 local-inference seam ---------------------------------------------------


class _FakeLocalDrafting:
    """Stand-in for an in-boundary local model (Ollama). Records that IT drafted."""

    def __init__(self):
        self.calls = []

    def produce(self, req, intent):
        from app.models import NoraResult, ResultKind

        self.calls.append(intent)
        return NoraResult(kind=ResultKind.DRAFT, text="LOCAL DRAFTED reply")


def _r3_settings() -> Settings:
    return Settings(
        external_llm_ack=True, anthropic_api_key="k", provider_api_key="k",
        webhook_basic_auth=WEBHOOK_CRED, postmark_sender="nora@example.com",
        ceo_registry={HASH_LOCAL: "ceo@example.com"},
        posture_map={"acme.com": "R3"},  # FORWARDED_THREAD counterparty is sarah@acme.com
    )


def test_r3_drafts_locally_when_backend_configured(fake_provider) -> None:
    from app.email.outbound import OutboundSender
    from app.main import Dependencies
    from app.pipeline import Pipeline
    from app.state.store import InMemoryStateStore

    class _BoomClaude:
        def produce(self, req, intent):
            raise AssertionError("R3 must NOT call the external Claude backend")

    s = _r3_settings()
    local = _FakeLocalDrafting()
    pipe = Pipeline(settings=s, drafting=_BoomClaude(),
                    store=InMemoryStateStore(s.state_ttl_seconds), local_drafting=local)
    deps = Dependencies(pipeline=pipe, outbound=OutboundSender(fake_provider))
    client = TestClient(create_app(s, deps=deps))
    resp = _post(client, MessageID="pm-r3-local", TextBody="RE" + FORWARDED_THREAD)
    assert resp.status_code == 202
    assert len(local.calls) == 1                                   # the local model drafted
    assert "LOCAL DRAFTED reply" in fake_provider.sent[-1]["body"]
    assert fake_provider.sent[-1]["to"] == "ceo@example.com"


def test_r3_declines_when_no_local_backend(fake_provider) -> None:
    # Default local_drafting=None → R3 declines exactly as today, no draft, no LLM call.
    from app.email.outbound import OutboundSender
    from app.main import Dependencies
    from app.pipeline import Pipeline
    from app.state.store import InMemoryStateStore

    class _BoomClaude:
        def produce(self, req, intent):
            raise AssertionError("a declined R3 relationship must not draft at all")

    s = _r3_settings()
    pipe = Pipeline(settings=s, drafting=_BoomClaude(), store=InMemoryStateStore(s.state_ttl_seconds))
    deps = Dependencies(pipeline=pipe, outbound=OutboundSender(fake_provider))
    client = TestClient(create_app(s, deps=deps))
    _post(client, MessageID="pm-r3-none", TextBody="RE" + FORWARDED_THREAD)
    body = fake_provider.sent[-1]["body"].lower()
    assert "higher-assurance" in body or "haven't processed" in body


def test_r3_declines_egress_intent_even_with_local_backend(fake_provider) -> None:
    # Producing intents draft locally; a third-party-egress intent (SM propose-meeting)
    # still declines at R3 in this version (egress is the deferred boundary).
    from app.email.outbound import OutboundSender
    from app.main import Dependencies
    from app.pipeline import Pipeline
    from app.state.store import InMemoryStateStore
    from tests.conftest import StubDrafting

    s = _r3_settings()
    local = _FakeLocalDrafting()
    pipe = Pipeline(settings=s, drafting=StubDrafting(),
                    store=InMemoryStateStore(s.state_ttl_seconds), local_drafting=local)
    deps = Dependencies(pipeline=pipe, outbound=OutboundSender(fake_provider))
    client = TestClient(create_app(s, deps=deps))
    _post(client, MessageID="pm-r3-sm", TextBody="SM" + FORWARDED_THREAD)
    body = fake_provider.sent[-1]["body"].lower()
    assert "higher-assurance" in body or "haven't processed" in body
    assert local.calls == []                                       # never drafted


def test_r3_local_backend_failure_declines_without_claude_fallback(fake_provider) -> None:
    # If the LOCAL backend fails, _draft_or_fail returns a graceful CLARIFY — it must
    # NEVER fall back to the external Claude backend (the R3 boundary holds on failure).
    from app.drafting.result import DraftingError
    from app.email.outbound import OutboundSender
    from app.main import Dependencies
    from app.pipeline import Pipeline
    from app.state.store import InMemoryStateStore

    class _BoomClaude:
        def produce(self, req, intent):
            raise AssertionError("R3 must NOT fall back to Claude when the local backend fails")

    class _FailingLocal:
        def produce(self, req, intent):
            raise DraftingError("local model unavailable")

    s = _r3_settings()
    pipe = Pipeline(settings=s, drafting=_BoomClaude(),
                    store=InMemoryStateStore(s.state_ttl_seconds), local_drafting=_FailingLocal())
    deps = Dependencies(pipeline=pipe, outbound=OutboundSender(fake_provider))
    client = TestClient(create_app(s, deps=deps))
    resp = _post(client, MessageID="pm-r3-fail", TextBody="RE" + FORWARDED_THREAD)
    assert resp.status_code == 202
    assert "couldn't draft that" in fake_provider.sent[-1]["body"].lower()


def test_r3_first_contact_notice_does_not_claim_claude(fake_provider) -> None:
    # Trust-critical: an R3 draft is produced by the LOCAL in-boundary model, so its
    # first-contact disclosure must NOT say content was processed by Claude/an external
    # service — that would contradict the whole R3 guarantee.
    from app.email.outbound import OutboundSender
    from app.main import Dependencies
    from app.pipeline import Pipeline
    from app.state.store import InMemoryStateStore
    from tests.conftest import StubDrafting

    s = _r3_settings()
    pipe = Pipeline(settings=s, drafting=StubDrafting(),
                    store=InMemoryStateStore(s.state_ttl_seconds), local_drafting=_FakeLocalDrafting())
    deps = Dependencies(pipeline=pipe, outbound=OutboundSender(fake_provider))
    client = TestClient(create_app(s, deps=deps))
    _post(client, MessageID="pm-r3-fc", TextBody="RE" + FORWARDED_THREAD)
    body = fake_provider.sent[-1]["body"]
    assert "First time using Nora" in body                  # a disclosure IS shown
    assert "Claude" not in body                             # but NOT the external-LLM claim
    assert "local AI model" in body                         # it is the in-boundary disclosure
    assert "never leaves to an external AI service" in body


def test_r1_first_contact_notice_still_names_the_external_service(fake_deps, fake_provider) -> None:
    # Regression: the R1 (external Claude) path keeps the original KTD5 disclosure.
    client = TestClient(create_app(_settings(), deps=fake_deps))
    _post(client)  # default WIYOW forward, R1
    body = fake_provider.sent[-1]["body"]
    assert "First time using Nora" in body
    assert "Claude" in body


def test_kl_code_forces_local_even_with_empty_posture(fake_provider) -> None:
    # The KL code keeps a message on-device regardless of counterparty: the default
    # acme.com counterparty would be R1 (Claude), but NB-RE-KL routes to the local model.
    from app.email.outbound import OutboundSender
    from app.main import Dependencies
    from app.pipeline import Pipeline
    from app.state.store import InMemoryStateStore

    class _BoomClaude:
        def produce(self, req, intent):
            raise AssertionError("a KL (keep-local) message must NOT reach the Cloud API")

    s = _settings()  # empty posture_map → acme.com is R1 by default
    local = _FakeLocalDrafting()
    pipe = Pipeline(settings=s, drafting=_BoomClaude(),
                    store=InMemoryStateStore(s.state_ttl_seconds), local_drafting=local)
    deps = Dependencies(pipeline=pipe, outbound=OutboundSender(fake_provider))
    client = TestClient(create_app(s, deps=deps))
    _post(client, MessageID="pm-kl", TextBody="NB-RE-KL" + FORWARDED_THREAD)
    assert len(local.calls) == 1                                   # drafted on-device
    body = fake_provider.sent[-1]["body"]
    assert "LOCAL DRAFTED reply" in body
    assert "Kept local (KL)" in body                              # per-email receipt


def test_kl_code_declines_when_no_local_backend(fake_provider) -> None:
    # Fail-safe: KL with no local backend declines — it must never fall back to the Cloud API.
    from app.email.outbound import OutboundSender
    from app.main import Dependencies
    from app.pipeline import Pipeline
    from app.state.store import InMemoryStateStore

    class _BoomClaude:
        def produce(self, req, intent):
            raise AssertionError("KL must never reach the Cloud API, even with no local backend")

    s = _settings()
    pipe = Pipeline(settings=s, drafting=_BoomClaude(), store=InMemoryStateStore(s.state_ttl_seconds))
    deps = Dependencies(pipeline=pipe, outbound=OutboundSender(fake_provider))
    client = TestClient(create_app(s, deps=deps))
    _post(client, MessageID="pm-kl-none", TextBody="NB-RE-KL" + FORWARDED_THREAD)
    body = fake_provider.sent[-1]["body"].lower()
    assert "isn't available" in body or "haven't processed" in body


def test_kl_is_sticky_revise_stays_local(fake_provider) -> None:
    # Stickiness: replying to a KL draft round-trips back through the on-device model —
    # the revise must NOT reach the Cloud API, and re-confirms "Kept local".
    from app.email.outbound import OutboundSender
    from app.main import Dependencies
    from app.pipeline import Pipeline
    from app.state.store import InMemoryStateStore

    class _BoomClaude:
        def produce(self, req, intent):
            raise AssertionError("a KL revise must NOT reach the Cloud API")

    s = _settings()
    local = _FakeLocalDrafting()
    pipe = Pipeline(settings=s, drafting=_BoomClaude(),
                    store=InMemoryStateStore(s.state_ttl_seconds), local_drafting=local)
    deps = Dependencies(pipeline=pipe, outbound=OutboundSender(fake_provider))
    client = TestClient(create_app(s, deps=deps))
    _post(client, MessageID="pm-kl1", TextBody="NB-RE-KL" + FORWARDED_THREAD)
    token = fake_provider.sent[-1]["reply_to"].split("+", 1)[1].split("@", 1)[0]
    assert len(local.calls) == 1
    # Reply to the KL draft (correlation token rides MailboxHash) → revise, still local.
    _post(client, MessageID="pm-kl1-rev", MailboxHash=token, TextBody="make it shorter")
    assert len(local.calls) == 2                                   # re-drafted on-device
    body = fake_provider.sent[-1]["body"]
    assert "LOCAL DRAFTED reply" in body
    assert "Kept local (KL)" in body                              # stickiness re-confirmed
    # Second revise round: stickiness must persist across the re-stored record (never Cloud).
    token2 = fake_provider.sent[-1]["reply_to"].split("+", 1)[1].split("@", 1)[0]
    _post(client, MessageID="pm-kl1-rev2", MailboxHash=token2, TextBody="warmer please")
    assert len(local.calls) == 3                                   # still on-device
    assert "Kept local (KL)" in fake_provider.sent[-1]["body"]


def _kl_local_pipe_deps(fake_provider):
    # Helper: a pipeline whose Cloud backend explodes if touched + a local fake.
    from app.email.outbound import OutboundSender
    from app.main import Dependencies
    from app.pipeline import Pipeline
    from app.state.store import InMemoryStateStore

    class _BoomClaude:
        def produce(self, req, intent):
            raise AssertionError("KL phrasing must NOT reach the Cloud API")

    s = _settings()
    local = _FakeLocalDrafting()
    pipe = Pipeline(settings=s, drafting=_BoomClaude(),
                    store=InMemoryStateStore(s.state_ttl_seconds), local_drafting=local)
    deps = Dependencies(pipeline=pipe, outbound=OutboundSender(fake_provider))
    return s, local, deps


def test_kl_natural_language_phrasing_forces_local(fake_provider) -> None:
    # Security: a natural-language instruction containing KL must keep the email local,
    # not leak to the Cloud (the strict leading-token parser used to drop it).
    s, local, deps = _kl_local_pipe_deps(fake_provider)
    client = TestClient(create_app(s, deps=deps))
    _post(client, MessageID="pm-kl-nl", TextBody="reply to this KL" + FORWARDED_THREAD)
    assert len(local.calls) == 1                                   # drafted on-device
    assert "Kept local (KL)" in fake_provider.sent[-1]["body"]


def test_kl_space_separated_forces_local(fake_provider) -> None:
    # Security: 'NB-RE KL' (space instead of dash — a likely typo) must still keep local.
    s, local, deps = _kl_local_pipe_deps(fake_provider)
    client = TestClient(create_app(s, deps=deps))
    _post(client, MessageID="pm-kl-sp", TextBody="NB-RE KL" + FORWARDED_THREAD)
    assert len(local.calls) == 1
    assert "Kept local (KL)" in fake_provider.sent[-1]["body"]


def test_without_kl_acme_still_uses_cloud(fake_deps, fake_provider) -> None:
    # Regression: a normal (no-KL) forward from acme.com still drafts via the Cloud path.
    client = TestClient(create_app(_settings(), deps=fake_deps))
    _post(client)  # default WIYOW forward, no KL
    assert "DRAFTED reply" in fake_provider.sent[-1]["body"]
    assert "Kept local" not in fake_provider.sent[-1]["body"]


def test_build_default_dependencies_wires_local_drafting_when_ollama() -> None:
    from app.main import build_default_dependencies

    s = Settings(
        external_llm_ack=True, anthropic_api_key="k", webhook_basic_auth=WEBHOOK_CRED,
        postmark_sender="nora@example.com", transport="smtp", smtp_host="localhost",
        local_drafting_backend="ollama",
    )
    deps = build_default_dependencies(s)
    assert deps.pipeline.local_drafting is not None                # an OllamaDraftingService


def test_build_default_dependencies_local_drafting_none_by_default() -> None:
    from app.main import build_default_dependencies

    s = Settings(
        external_llm_ack=True, anthropic_api_key="k", webhook_basic_auth=WEBHOOK_CRED,
        postmark_sender="nora@example.com", transport="smtp", smtp_host="localhost",
    )
    deps = build_default_dependencies(s)
    assert deps.pipeline.local_drafting is None


def test_parse_request_extracts_attachment_text_and_strips_bytes() -> None:
    from app.email.parser import parse_request
    from app.models import Attachment, InboundEmail
    email = InboundEmail(
        event_id="e", secret_address_local="nora", from_address="ceo@x.com",
        dkim_verified=True, subject="S", body="S\n\nFrom: a@b.com\nseen it?",
        attachments=[Attachment(filename="plan.txt", size=11, content=b"GROW 20pct")],
    )
    req = parse_request(email)
    assert any(e.status == "read" and "GROW" in e.text for e in req.thread.attachment_extracts)
    assert req.thread.attachments[0].content is None  # bytes not persisted


def test_build_user_content_includes_attachments_and_unread_note() -> None:
    from app.drafting.prompts import build_user_content
    from app.interpreter import Intent
    from app.email.parser import parse_request
    from app.models import ExtractedAttachment, InboundEmail
    email = InboundEmail(event_id="e", secret_address_local="nora",
                         from_address="ceo@x.com", dkim_verified=True, body="S")
    req = parse_request(email)
    req.thread.attachment_extracts = [
        ExtractedAttachment(filename="plan.txt", status="read", text="GROW 20pct"),
        ExtractedAttachment(filename="scan.pdf", status="unreadable", reason="no_text_found"),
    ]
    content = build_user_content(req, Intent.SUMMARIZE)
    assert "<attachments>" in content and "GROW 20pct" in content
    assert "scan.pdf" in content and "no_text_found" in content


def test_report_file_format_attaches_office_file(fake_deps, fake_provider) -> None:
    import io
    import zipfile
    client = TestClient(create_app(_settings(), deps=fake_deps))
    resp = _post(client, MessageID="pm-prw", TextBody="NB-PR-W" + FORWARDED_THREAD)
    assert resp.status_code == 202
    sent = fake_provider.sent[-1]
    assert sent["to"] == "ceo@example.com"
    [att] = sent["attachments"]
    assert att.filename.endswith(".docx")
    assert zipfile.is_zipfile(io.BytesIO(att.content))  # valid OOXML container


def test_report_without_format_has_no_attachment(fake_deps, fake_provider) -> None:
    client = TestClient(create_app(_settings(), deps=fake_deps))
    _post(client, MessageID="pm-pr", TextBody="NB-PR" + FORWARDED_THREAD)
    assert not fake_provider.sent[-1]["attachments"]


def test_calendar_propose_parses_concrete_time_into_hold(fake_deps, fake_provider) -> None:
    client = TestClient(create_app(_settings(), deps=fake_deps))
    _post(client, MessageID="pm-smA",
          TextBody="NB-SM-A [meet on 2026-07-01 15:00]" + FORWARDED_THREAD)
    body = fake_provider.sent[-1]["body"]
    assert "2026-07-01T15:00" in body  # concrete start, not the placeholder
    assert "time to be confirmed" not in body


def test_memory_records_on_r1_draft_and_recalls(bootable_settings, tmp_path) -> None:
    from app.memory import FileMemoryStore
    from app.models import InboundEmail
    from app.pipeline import Pipeline
    from app.state.store import InMemoryStateStore
    from tests.conftest import StubDrafting

    mem = FileMemoryStore(str(tmp_path / "mem.json"))
    pipe = Pipeline(settings=bootable_settings, drafting=StubDrafting(),
                    store=InMemoryStateStore(bootable_settings.state_ttl_seconds), memory=mem)
    email = InboundEmail(event_id="m1", secret_address_local=HASH_LOCAL,
                         from_address="ceo@example.com", dkim_verified=True,
                         subject="Fwd", body="RE" + FORWARDED_THREAD)
    pipe.handle(email)
    rec = mem.recall(HASH_LOCAL, "sarah@acme.com")
    assert rec is not None and rec.thread_count == 1  # R1 draft recorded the counterparty


def test_memory_not_recorded_on_keep_local(bootable_settings, tmp_path) -> None:
    # KL with no local backend declines BEFORE drafting → must never touch the shared store.
    from app.memory import FileMemoryStore
    from app.models import InboundEmail
    from app.pipeline import Pipeline
    from app.state.store import InMemoryStateStore
    from tests.conftest import StubDrafting

    mem = FileMemoryStore(str(tmp_path / "mem.json"))
    pipe = Pipeline(settings=bootable_settings, drafting=StubDrafting(),
                    store=InMemoryStateStore(bootable_settings.state_ttl_seconds), memory=mem)
    email = InboundEmail(event_id="m2", secret_address_local=HASH_LOCAL,
                         from_address="ceo@example.com", dkim_verified=True,
                         subject="Fwd", body="NB-RE-KL" + FORWARDED_THREAD)
    pipe.handle(email)
    assert mem.recall(HASH_LOCAL, "sarah@acme.com") is None  # keep-local never recorded


def test_relationship_context_rendered_fenced_in_prompt() -> None:
    from app.drafting.prompts import build_user_content
    from app.interpreter import Intent
    from app.models import InboundEmail
    from app.email.parser import parse_request

    req = parse_request(InboundEmail(event_id="e", secret_address_local="nora",
                                     from_address="ceo@x.com", dkim_verified=True, body="RE"))
    req.relationship_context = "prior exchanges with this counterparty: 3; last 2026-05-30"
    content = build_user_content(req, Intent.DRAFT)
    assert "<relationship_context>" in content and "prior exchanges" in content


def test_sm_a_iv_proposes_invite_and_confirm_invites_participant(fake_deps, fake_provider) -> None:
    fake_cal = _FakeCalendarClient()
    fake_deps.calendar = fake_cal
    client = TestClient(create_app(_settings(), deps=fake_deps))
    # Propose: SM.A.IV with a concrete time → proposal says it will INVITE the participant.
    resp = _post(client, MessageID="pm-iv",
                 TextBody="NB-SM-A-IV [meet 2026-07-01 15:00]" + FORWARDED_THREAD)
    assert resp.status_code == 202
    proposal = fake_provider.sent[-1]
    assert "invite" in proposal["body"].lower() and "sarah@acme.com" in proposal["body"]
    assert all(s["to"] != "sarah@acme.com" for s in fake_provider.sent)  # nothing yet
    token = proposal["reply_to"].split("+", 1)[1].split("@", 1)[0]
    # Confirm (AG) → calendar event created WITH an invitation sent.
    _post(client, MessageID="pm-iv-ag", TextBody="AG",
          OriginalRecipient=f"{HASH_LOCAL}+{token}@inbound.postmarkapp.com")
    assert fake_cal.invites == [True]


def test_sm_a_without_iv_is_private_hold(fake_deps, fake_provider) -> None:
    fake_cal = _FakeCalendarClient()
    fake_deps.calendar = fake_cal
    client = TestClient(create_app(_settings(), deps=fake_deps))
    _post(client, MessageID="pm-ph", TextBody="NB-SM-A [meet 2026-07-01 15:00]" + FORWARDED_THREAD)
    proposal = fake_provider.sent[-1]
    assert "private hold" in proposal["body"].lower()
    token = proposal["reply_to"].split("+", 1)[1].split("@", 1)[0]
    _post(client, MessageID="pm-ph-ag", TextBody="AG",
          OriginalRecipient=f"{HASH_LOCAL}+{token}@inbound.postmarkapp.com")
    assert fake_cal.invites == [False]  # private hold, no invitation sent


def test_file_reference_reads_into_draft_when_source_configured(bootable_settings, tmp_path) -> None:
    from app.email.parser import parse_request
    from app.drafting.prompts import build_user_content
    from app.interpreter import Intent
    from app.models import InboundEmail

    class _StubSource:
        def read(self, ref):
            return "GROSS MARGIN 62%" if ref == "Q3-deck.pdf" else None

    from app.pipeline import _file_ref
    req = parse_request(InboundEmail(event_id="e", secret_address_local="nora",
                                     from_address="ceo@x.com", dkim_verified=True,
                                     body="PR [file: Q3-deck.pdf]"))
    ref = _file_ref(req.instruction_text)
    assert ref == "Q3-deck.pdf"
    req.file_context = f"{ref}\n{_StubSource().read(ref)}"
    content = build_user_content(req, Intent.REPORT)
    assert "<file>" in content and "GROSS MARGIN 62%" in content


def test_file_ref_absent_yields_none() -> None:
    from app.pipeline import _file_ref
    assert _file_ref("just summarize this") is None
    assert _file_ref(None) is None


class _StubFileSource:
    def __init__(self, files: dict) -> None:
        self._files = files

    def read(self, ref):
        b = self._files.get(ref)
        return b.decode(errors="replace") if b else None

    def fetch(self, ref):
        if ref not in self._files:
            return None
        from app.models import OutboundAttachment
        return OutboundAttachment(filename=ref, content=self._files[ref], content_type="application/pdf")

    def grant(self, ref, recipient):
        return f"https://store.example/{ref}" if ref in self._files else None

    def search(self, ref):
        return [ref] if ref in self._files else []


def _share_deps(fake_deps, files):
    src = _StubFileSource(files)
    fake_deps.file_source = src
    fake_deps.pipeline.file_source = src
    return fake_deps


def test_share_proposes_then_ag_sends_file_attachment(fake_deps, fake_provider) -> None:
    _share_deps(fake_deps, {"deck.pdf": b"%PDF-1.4 fake"})
    client = TestClient(create_app(_settings(), deps=fake_deps))
    resp = _post(client, MessageID="pm-sh", TextBody="NB-SH [file: deck.pdf]" + FORWARDED_THREAD)
    assert resp.status_code == 202
    proposal = fake_provider.sent[-1]
    assert proposal["to"] == "ceo@example.com" and "deck.pdf" in proposal["body"]
    assert all(s["to"] != "sarah@acme.com" for s in fake_provider.sent)  # nothing sent yet
    token = proposal["reply_to"].split("+", 1)[1].split("@", 1)[0]
    # AG → file re-fetched and sent as an attachment from Nora's address.
    _post(client, MessageID="pm-sh-ag", TextBody="AG",
          OriginalRecipient=f"{HASH_LOCAL}+{token}@inbound.postmarkapp.com")
    shared = [s for s in fake_provider.sent if s["to"] == "sarah@acme.com"]
    assert len(shared) == 1
    assert shared[0]["attachments"] and shared[0]["attachments"][0].filename == "deck.pdf"


def test_share_clarifies_when_file_missing(fake_deps, fake_provider) -> None:
    _share_deps(fake_deps, {})
    client = TestClient(create_app(_settings(), deps=fake_deps))
    _post(client, MessageID="pm-sh3", TextBody="NB-SH [file: ghost.pdf]" + FORWARDED_THREAD)
    assert "couldn't find" in fake_provider.sent[-1]["body"].lower()
    assert all(s["to"] != "sarah@acme.com" for s in fake_provider.sent)


def test_share_clarifies_without_file_ref(fake_deps, fake_provider) -> None:
    _share_deps(fake_deps, {"deck.pdf": b"x"})
    client = TestClient(create_app(_settings(), deps=fake_deps))
    _post(client, MessageID="pm-sh4", TextBody="NB-SH" + FORWARDED_THREAD)
    assert "which file" in fake_provider.sent[-1]["body"].lower()


def test_file_ref_robust_to_trailing_notes() -> None:
    from app.pipeline import _file_ref
    # The parser strips [brackets]; a trailing note must not leak into the filename.
    assert _file_ref("file: deck.pdf note: urgent") == "deck.pdf"
    assert _file_ref("file: deck.pdf with the numbers") == "deck.pdf"
    assert _file_ref("file: Q3 deck.pdf") == "Q3 deck.pdf"   # spaces in name, ends at ext
    assert _file_ref("file: 'my report.pdf'") == "my report.pdf"  # quoted
    assert _file_ref("file: Notes extra words") == "Notes"   # no ext → single token


def test_share_fails_closed_when_file_missing_at_send(fake_deps, fake_provider) -> None:
    # File exists at propose, then disappears before AG → no share goes out, CEO gets a
    # failure receipt (never an empty/attachment-less send).
    class _Vanishing:
        # Found at propose (search), but gone/unfetchable by send → must fail closed.
        def read(self, ref):
            return "x"

        def search(self, ref):
            return [ref]

        def fetch(self, ref):
            return None

        def grant(self, ref, recipient):
            return None

    src = _Vanishing()
    fake_deps.file_source = src
    fake_deps.pipeline.file_source = src
    client = TestClient(create_app(_settings(), deps=fake_deps))
    _post(client, MessageID="pm-shv", TextBody="NB-SH [file: deck.pdf]" + FORWARDED_THREAD)
    token = fake_provider.sent[-1]["reply_to"].split("+", 1)[1].split("@", 1)[0]
    _post(client, MessageID="pm-shv-ag", TextBody="AG",
          OriginalRecipient=f"{HASH_LOCAL}+{token}@inbound.postmarkapp.com")
    assert all(s["to"] != "sarah@acme.com" for s in fake_provider.sent)  # nothing sent out
    assert any("couldn't send" in s["subject"].lower() for s in fake_provider.sent
               if s["to"] == "ceo@example.com")  # CEO told it failed


def test_r3_file_access_reads_from_local_vault_only(bootable_settings, tmp_path) -> None:
    # R3 (keep-local) file access reads from the in-boundary FileVault, never the Cloud source.
    from app.filesource import LocalFileVault
    from app.models import InboundEmail
    from app.pipeline import Pipeline
    from app.state.store import InMemoryStateStore
    from tests.conftest import StubDrafting

    (tmp_path / "deck.txt").write_text("LOCAL ONLY: margin 62%")

    class _CloudShouldNotBeUsed:
        def read(self, ref):
            raise AssertionError("R3 must not use the Cloud file source")
        def fetch(self, ref):
            raise AssertionError("R3 must not use the Cloud file source")

    captured = {}

    class _CapturingLocal(StubDrafting):
        def produce(self, req, intent):
            captured["file_context"] = req.file_context
            return super().produce(req, intent)

    pipe = Pipeline(
        settings=bootable_settings, drafting=StubDrafting(),
        store=InMemoryStateStore(bootable_settings.state_ttl_seconds),
        local_drafting=_CapturingLocal(),
        file_source=_CloudShouldNotBeUsed(),
        local_file_source=LocalFileVault(str(tmp_path)),
    )
    email = InboundEmail(event_id="e", secret_address_local=HASH_LOCAL,
                         from_address="ceo@example.com", dkim_verified=True,
                         subject="Fwd", body="NB-S-KL [file: deck.txt]" + FORWARDED_THREAD)
    pipe.handle(email)
    assert captured.get("file_context") and "LOCAL ONLY: margin 62%" in captured["file_context"]


def test_share_grant_grants_access_and_emails_link_after_ag(fake_deps, fake_provider) -> None:
    _share_deps(fake_deps, {"deck.pdf": b"%PDF-1.4 fake"})
    client = TestClient(create_app(_settings(), deps=fake_deps))
    _post(client, MessageID="pm-shg", TextBody="NB-SH-G [file: deck.pdf]" + FORWARDED_THREAD)
    proposal = fake_provider.sent[-1]
    assert proposal["to"] == "ceo@example.com" and "access" in proposal["body"].lower()
    token = proposal["reply_to"].split("+", 1)[1].split("@", 1)[0]
    _post(client, MessageID="pm-shg-ag", TextBody="AG",
          OriginalRecipient=f"{HASH_LOCAL}+{token}@inbound.postmarkapp.com")
    shared = [s for s in fake_provider.sent if s["to"] == "sarah@acme.com"]
    assert len(shared) == 1
    assert "https://store.example/deck.pdf" in shared[0]["body"]  # the link
    assert not shared[0]["attachments"]  # grant mode: a link, not an attachment


def test_share_disambiguates_multiple_matches(fake_deps, fake_provider) -> None:
    class _MultiMatch:
        def search(self, ref):
            return ["Q3-deck-v1.pdf", "Q3-deck-v2.pdf"]  # ambiguous
        def fetch(self, ref):
            from app.models import OutboundAttachment
            return OutboundAttachment(filename=ref, content=b"x", content_type="application/pdf")
        def read(self, ref): return "x"
        def grant(self, ref, recipient): return "https://x"

    src = _MultiMatch()
    fake_deps.file_source = src
    fake_deps.pipeline.file_source = src
    client = TestClient(create_app(_settings(), deps=fake_deps))
    _post(client, MessageID="pm-shm", TextBody="NB-SH [file: Q3-deck]" + FORWARDED_THREAD)
    body = fake_provider.sent[-1]["body"]
    assert "several files" in body.lower()
    assert "Q3-deck-v1.pdf" in body and "Q3-deck-v2.pdf" in body  # lists candidates
    assert all(s["to"] != "sarah@acme.com" for s in fake_provider.sent)  # nothing shared


def test_access_disambiguation_uses_best_match_and_flags_it() -> None:
    from app.pipeline import _read_referenced_file

    class _Multi:
        def search(self, ref):
            return ["a-v1.txt", "a-v2.txt"]
        def read(self, ref):
            return f"content of {ref}"

    out = _read_referenced_file(_Multi(), "a")
    assert out is not None
    assert "a-v1.txt" in out and "matched several files" in out
    assert "content of a-v1.txt" in out  # best match read; ambiguity flagged


def test_access_single_match_has_no_note() -> None:
    from app.pipeline import _read_referenced_file

    class _One:
        def search(self, ref):
            return ["only.txt"]
        def read(self, ref):
            return "the text"

    out = _read_referenced_file(_One(), "only.txt")
    assert out == "only.txt\nthe text"  # no ambiguity note


def test_nl_file_descriptor_extracts_from_plain_requests() -> None:
    from app.pipeline import _nl_file_descriptor
    assert _nl_file_descriptor("please get me the file of Q4's earnings and summarize it") == "Q4's earnings"
    assert _nl_file_descriptor("find the Q3 board deck file") == "Q3 board deck"
    assert _nl_file_descriptor("pull up the partnership contract file") == "partnership contract"
    # "summarize/read/retrieve the X file" (acting on, not just fetching) must also resolve.
    assert _nl_file_descriptor("can you summarize the earnings file?") == "earnings"
    assert _nl_file_descriptor("read the Q4 earnings file and summarize") == "Q4 earnings"
    # The LLM interpreter paraphrases with verbs like "retrieve" — must still resolve.
    assert _nl_file_descriptor("Retrieve the Q4 earnings file and provide a summary.") == "Q4 earnings"
    assert _nl_file_descriptor("just summarize this thread") is None


def test_file_ref_mined_from_original_when_interpreter_paraphrases(fake_deps, fake_provider) -> None:
    # The interpreter rewrites instruction_text into a paraphrase that DROPS the file
    # reference; the original request must still be mined so the file is found + read.
    from app.nl_interpreter import InterpretedRequest

    class _Paraphraser:
        def interpret(self, text):
            return InterpretedRequest(intent="summarize", notes="Summarize the document concisely.")

    class _Drive:
        def search(self, ref):
            return ["Q4-2026-earnings.docx"] if "earnings" in ref.lower() else []
        def read(self, ref):
            return "Revenue $6.1M, a record quarter."

    fake_deps.pipeline.interpreter = _Paraphraser()
    fake_deps.pipeline.file_source = _Drive()
    captured = {}
    orig = fake_deps.pipeline.drafting.produce

    def _spy(req, intent):
        captured["file_context"] = req.file_context
        return orig(req, intent)

    fake_deps.pipeline.drafting.produce = _spy
    client = TestClient(create_app(_settings(), deps=fake_deps))
    _post(client, MessageID="pm-para",
          TextBody="Please get me the file of Q4's earnings and summarize it." + FORWARDED_THREAD)
    assert captured.get("file_context") and "Revenue $6.1M" in captured["file_context"]


def test_nl_descriptor_feeds_drive_search() -> None:
    from app.pipeline import _nl_file_descriptor, _read_referenced_file

    class _Drive:
        def search(self, ref):
            return ["Q4-earnings.pdf"] if "earnings" in ref.lower() else []
        def read(self, ref):
            return "Net income up 18%."

    desc = _nl_file_descriptor("get me the file of Q4 earnings, read it and summarize it")
    out = _read_referenced_file(_Drive(), desc)
    assert out is not None and "Net income up 18%" in out


def test_file_disambiguation_clarifies_on_multiple_matches(fake_deps, fake_provider) -> None:
    # "She won't guess — no wrong file": a fuzzy ref matching several files yields a
    # clarifying reply that lists them and reads none (deterministic, no LLM summary).
    class _Multi:
        names = ["Q1-2026-earnings.txt", "Q2-2026-earnings.txt",
                 "Q3-2026-earnings.txt", "Q4-2026-earnings.docx"]

        def search(self, ref):
            return list(self.names)

        def read(self, ref):
            return "SHOULD NOT BE READ"

    fake_deps.pipeline.file_source = _Multi()
    client = TestClient(create_app(_settings(), deps=fake_deps))
    _post(client, MessageID="pm-amb", TextBody="S [file: earnings]" + FORWARDED_THREAD)
    body = fake_provider.sent[-1]["body"]
    assert "Q1-2026-earnings.txt" in body and "Q4-2026-earnings.docx" in body
    assert "SHOULD NOT BE READ" not in body  # never summarized one


def test_file_single_fuzzy_match_reads_into_draft(fake_deps, fake_provider) -> None:
    # A single match resolves straight to the draft context — the happy-path summary.
    class _One:
        def search(self, ref):
            return ["Q4-2026-earnings.docx"]

        def read(self, ref):
            return "Revenue: $6.1M (+18% YoY), a record quarter."

    fake_deps.pipeline.file_source = _One()
    client = TestClient(create_app(_settings(), deps=fake_deps))
    _post(client, MessageID="pm-one", TextBody="S [file: Q4 earnings]" + FORWARDED_THREAD)
    body = fake_provider.sent[-1]["body"].lower()
    assert "won't guess" not in body and "which" not in body  # not a disambiguation


def test_report_sv_saves_to_drive_and_links(fake_deps, fake_provider) -> None:
    class _Creator:
        def create(self, filename, content, content_type):
            return "https://drive/r"
        def read(self, ref): return None
        def search(self, ref): return []
    fake_deps.pipeline.file_source = _Creator()
    client = TestClient(create_app(_settings(), deps=fake_deps))
    _post(client, MessageID="pm-sv", TextBody="NB-PR-W-SV" + FORWARDED_THREAD)
    assert "https://drive/r" in fake_provider.sent[-1]["body"]


def test_voice_recorded_and_used_on_r1(fake_deps, fake_provider) -> None:
    from app.models import NoraResult, ResultKind

    seen = {}

    class _Voice:
        def __init__(self):
            self.recorded = []

        def record(self, ceo, codes, when, counterparty=""):
            self.recorded.append((ceo, tuple(codes), counterparty))

        def profile(self, ceo, counterparty=""):
            return "" if counterparty else "Preferred style: concise."

        def signals(self, ceo, counterparty=""):
            return []

        def set_profile(self, ceo, t, counterparty=""):
            pass

        def forget(self, ceo):
            pass

    class _CapDraft:
        def produce(self, req, intent):
            seen["voice"] = req.voice_profile
            return NoraResult(
                kind=ResultKind.DRAFT, text="DRAFTED", interpretation_readback="x",
                recipient=req.thread.original_sender, recipient_uncertain=req.recipient_uncertain,
            )

    fake_deps.pipeline.voice = _Voice()
    fake_deps.pipeline.drafting = _CapDraft()
    client = TestClient(create_app(_settings(), deps=fake_deps))
    _post(client, MessageID="pm-voice", TextBody="NB-RE-Short-F" + FORWARDED_THREAD)
    assert fake_deps.pipeline.voice.recorded
    assert any("SHORT" in codes for _ceo, codes, _cp in fake_deps.pipeline.voice.recorded)
    # v2: signals recorded both globally and under the known counterparty.
    assert any(cp for _ceo, _codes, cp in fake_deps.pipeline.voice.recorded)
    assert seen["voice"] == "Preferred style: concise."


def test_vp_views_and_forgets(fake_deps, fake_provider) -> None:
    class _Voice:
        def __init__(self):
            self.prof = "Preferred style: concise."
            self.forgot = False

        def record(self, *a):
            pass

        def profile(self, ceo):
            return self.prof

        def signals(self, ceo):
            return []

        def set_profile(self, ceo, t):
            pass

        def forget(self, ceo):
            self.forgot = True
            self.prof = ""

    v = _Voice()
    fake_deps.pipeline.voice = v
    client = TestClient(create_app(_settings(), deps=fake_deps))
    _post(client, MessageID="pm-vp1", TextBody="NB-VP")
    assert "Preferred style: concise." in fake_provider.sent[-1]["body"]
    _post(client, MessageID="pm-vp2", TextBody="NB-VP FORGET")
    assert v.forgot is True


TRIAGE_BUNDLE = (
    "NB-TR\n---------- Forwarded message ----------\nFrom: a@x.com\nThread A\n"
    "---------- Forwarded message ----------\nFrom: b@y.com\nThread B"
)


def test_tr_returns_readonly_digest(fake_deps, fake_provider) -> None:
    from app.models import Intent, NoraResult, ResultKind

    seen = {}

    class _CapDraft:
        def produce(self, req, intent):
            seen["intent"] = intent
            seen["items"] = list(req.triage_items)
            return NoraResult(kind=ResultKind.SUMMARY, text="Triaged 2 threads. ...",
                              interpretation_readback="Triage.")

    fake_deps.pipeline.drafting = _CapDraft()
    client = TestClient(create_app(_settings(), deps=fake_deps))
    _post(client, MessageID="pm-tr", TextBody=TRIAGE_BUNDLE)
    assert seen["intent"] is Intent.TRIAGE and len(seen["items"]) == 2
    assert "Triaged 2 threads" in fake_provider.sent[-1]["body"]


def _triage_capture_pipe(bootable_settings, memory=None):
    from app.models import NoraResult, ResultKind
    from app.pipeline import Pipeline
    from app.state.store import InMemoryStateStore

    seen: dict = {}

    class CapDraft:
        def produce(self, req, intent):
            seen["hints"] = list(req.triage_relationship_hints)
            return NoraResult(kind=ResultKind.SUMMARY, text="Triaged 2 threads.",
                              interpretation_readback="T")

    pipe = Pipeline(settings=bootable_settings, drafting=CapDraft(),
                    store=InMemoryStateStore(bootable_settings.state_ttl_seconds), memory=memory)
    return pipe, seen


def test_triage_annotates_recurring_vs_first_time_sender(bootable_settings, tmp_path) -> None:
    from app.memory import FileMemoryStore
    from app.models import InboundEmail

    mem = FileMemoryStore(str(tmp_path / "mem.json"))
    mem.record(HASH_LOCAL, "a@x.com", "2026-06-20")  # a@x.com is a recurring sender
    pipe, seen = _triage_capture_pipe(bootable_settings, memory=mem)
    pipe.handle(InboundEmail(event_id="tr-rel", secret_address_local=HASH_LOCAL,
                             from_address="ceo@example.com", dkim_verified=True, subject="Fwd",
                             body=TRIAGE_BUNDLE))
    assert "recurring sender" in seen["hints"][0]
    assert seen["hints"][1] == "first-time sender"


def test_triage_no_hints_when_memory_off(bootable_settings) -> None:
    from app.models import InboundEmail

    pipe, seen = _triage_capture_pipe(bootable_settings)  # default NullMemoryStore (off)
    pipe.handle(InboundEmail(event_id="tr-off", secret_address_local=HASH_LOCAL,
                             from_address="ceo@example.com", dkim_verified=True, subject="Fwd",
                             body=TRIAGE_BUNDLE))
    assert seen["hints"] == []  # memory off → digest unchanged


def test_kl_plus_tr_declines(fake_deps, fake_provider) -> None:
    client = TestClient(create_app(_settings(), deps=fake_deps))
    _post(client, MessageID="pm-trkl",
          TextBody="NB-TR-KL\n---------- Forwarded message ----------\nx")
    body = fake_provider.sent[-1]["body"].lower()
    assert "keep" in body or "local" in body or "cloud" in body  # declined, not triaged


def _signup_payload(from_addr, *, dkim_domain=None, mid="pm-signup"):
    dom = dkim_domain or from_addr.split("@", 1)[1]
    return dict(
        MessageID=mid,
        OriginalRecipient="signup@inbound.postmarkapp.com",
        From=from_addr,
        FromFull={"Email": from_addr, "Name": "X"},
        Subject="Hi",
        TextBody="I'd like to try Nora.",
        MailboxHash="",
        Headers=[{"Name": "Authentication-Results",
                  "Value": f"mx; dkim=pass header.d={dom}; spf=pass; dmarc=pass"}],
        Attachments=[],
    )


def test_signup_provisions_and_welcomes(fake_deps, fake_provider, tmp_path) -> None:
    from app.users import FileUserStore
    s = _settings()
    s.signup_allowlist = ["@acme.com"]
    path = str(tmp_path / "u.json")
    fake_deps.pipeline.users = FileUserStore(path)
    fake_deps.pipeline.settings = s
    client = TestClient(create_app(s, deps=fake_deps))
    _post(client, **_signup_payload("founder@acme.com"))
    body = fake_provider.sent[-1]["body"]
    assert "nora-" in body and "@example.com" in body  # provisioned address in the welcome
    assert FileUserStore(path).find_by_sender("founder@acme.com") is not None


def test_signup_off_allowlist_declines(fake_deps, fake_provider, tmp_path) -> None:
    from app.users import FileUserStore
    s = _settings()
    s.signup_allowlist = ["@acme.com"]
    path = str(tmp_path / "u.json")
    fake_deps.pipeline.users = FileUserStore(path)
    fake_deps.pipeline.settings = s
    client = TestClient(create_app(s, deps=fake_deps))
    _post(client, **_signup_payload("stranger@evil.com", mid="pm-deny"))
    assert "access" in fake_provider.sent[-1]["body"].lower()
    assert FileUserStore(path).all() == {}  # not provisioned


def test_signup_without_dkim_is_rejected(fake_deps, fake_provider, tmp_path) -> None:
    from app.users import FileUserStore
    s = _settings()
    s.signup_allowlist = ["@acme.com"]
    path = str(tmp_path / "u.json")
    fake_deps.pipeline.users = FileUserStore(path)
    fake_deps.pipeline.settings = s
    client = TestClient(create_app(s, deps=fake_deps))
    # DKIM domain misaligned with the From domain -> not DKIM-verified -> rejected, no provisioning
    _post(client, **_signup_payload("founder@acme.com", dkim_domain="evil.com", mid="pm-nodkim"))
    assert FileUserStore(path).all() == {} and not fake_provider.sent


def test_help_returns_code_guide_with_signature(fake_deps, fake_provider) -> None:
    client = TestClient(create_app(_settings(), deps=fake_deps))
    _post(client, MessageID="pm-help", TextBody="HELP")
    body = fake_provider.sent[-1]["body"]
    assert "plain english" in body.lower() and "TR" in body and "KL" in body  # plain-language-first guide
    assert "https://docs.nora-bennett.com/" in body  # the all-emails signature


def test_every_nora_email_carries_the_signature(fake_deps, fake_provider) -> None:
    client = TestClient(create_app(_settings(), deps=fake_deps))
    _post(client, MessageID="pm-sig", TextBody="WIYOW" + FORWARDED_THREAD)
    body = fake_provider.sent[-1]["body"]
    assert "https://docs.nora-bennett.com/" in body and "AI executive assistant" in body


TRIAGE_BUNDLE_4 = (
    "NB-TR\n"
    "---------- Forwarded message ----------\nFrom: a@x.com\nThread A body\n"
    "---------- Forwarded message ----------\nFrom: b@x.com\nThread B body\n"
    "---------- Forwarded message ----------\nFrom: c@x.com\nThread C body\n"
    "---------- Forwarded message ----------\nFrom: d@x.com\nThread D body\n"
)


def test_triage_v2_followup_acts_on_chosen_items(fake_deps, fake_provider) -> None:
    client = TestClient(create_app(_settings(), deps=fake_deps))
    _post(client, MessageID="pm-tr", TextBody=TRIAGE_BUNDLE_4)
    digest = fake_provider.sent[-1]
    assert digest["reply_to"] and "+" in digest["reply_to"]  # digest now correlates for follow-up
    token = digest["reply_to"].split("+", 1)[1].split("@", 1)[0]
    _post(client, MessageID="pm-tr-fu", MailboxHash=token, TextBody="2 RE\n4 S")
    body = fake_provider.sent[-1]["body"]
    assert "item 2 (RE)" in body and "DRAFTED reply" in body
    assert "item 4 (S)" in body and "SUMMARY" in body


def test_triage_v2_followup_degrades_out_of_range_and_unsupported(fake_deps, fake_provider) -> None:
    client = TestClient(create_app(_settings(), deps=fake_deps))
    _post(client, MessageID="pm-tr2", TextBody=TRIAGE_BUNDLE_4)
    token = fake_provider.sent[-1]["reply_to"].split("+", 1)[1].split("@", 1)[0]
    _post(client, MessageID="pm-tr2-fu", MailboxHash=token, TextBody="9 RE\n1 SM")
    body = fake_provider.sent[-1]["body"]
    assert "no such item" in body                 # item 9 is out of range
    assert "not available per-item" in body       # SM is consent-gated, not a per-item action
    # nothing was proposed/sent to a third party from a triage follow-up
    assert all("i'm nora, ea to the ceo" not in (s.get("body") or "").lower() for s in fake_provider.sent)


def test_layerc_remember_stores_fact_on_r1(bootable_settings, tmp_path) -> None:
    from app.memory import FileMemoryStore
    from app.models import InboundEmail
    from app.pipeline import Pipeline
    from app.state.store import InMemoryStateStore
    from tests.conftest import StubDrafting

    mem = FileMemoryStore(str(tmp_path / "mem.json"))
    pipe = Pipeline(settings=bootable_settings, drafting=StubDrafting(),
                    store=InMemoryStateStore(bootable_settings.state_ttl_seconds), memory=mem)
    email = InboundEmail(event_id="rc1", secret_address_local=HASH_LOCAL,
                         from_address="ceo@example.com", dkim_verified=True, subject="Fwd",
                         body="RE [remember: prefers morning meetings]" + FORWARDED_THREAD)
    pipe.handle(email)
    rec = mem.recall(HASH_LOCAL, "sarah@acme.com")
    assert rec is not None and "prefers morning meetings" in rec.notes


def test_layerc_recalls_facts_into_draft_context(bootable_settings, tmp_path) -> None:
    from app.memory import FileMemoryStore
    from app.models import InboundEmail, NoraResult, ResultKind
    from app.pipeline import Pipeline
    from app.state.store import InMemoryStateStore

    seen: dict = {}

    class CapturingDrafting:
        def produce(self, req, intent):
            seen["ctx"] = req.relationship_context
            return NoraResult(kind=ResultKind.DRAFT, text="DRAFTED", interpretation_readback="")

    mem = FileMemoryStore(str(tmp_path / "mem.json"))
    mem.remember(HASH_LOCAL, "sarah@acme.com", "prefers morning meetings")
    pipe = Pipeline(settings=bootable_settings, drafting=CapturingDrafting(),
                    store=InMemoryStateStore(bootable_settings.state_ttl_seconds), memory=mem)
    email = InboundEmail(event_id="rc2", secret_address_local=HASH_LOCAL,
                         from_address="ceo@example.com", dkim_verified=True, subject="Fwd",
                         body="RE" + FORWARDED_THREAD)
    pipe.handle(email)
    assert seen["ctx"] and "prefers morning meetings" in seen["ctx"]


def test_layerc_v2_recalls_derived_with_unconfirmed_framing(bootable_settings, tmp_path) -> None:
    from app.memory import FileMemoryStore
    from app.models import InboundEmail, NoraResult, ResultKind
    from app.pipeline import Pipeline
    from app.state.store import InMemoryStateStore

    seen: dict = {}

    class CapturingDrafting:
        def produce(self, req, intent):
            seen["ctx"] = req.relationship_context
            seen["autolearn"] = req.autolearn
            return NoraResult(kind=ResultKind.DRAFT, text="DRAFTED", interpretation_readback="")

    bootable_settings.relationship_autolearn = True
    mem = FileMemoryStore(str(tmp_path / "mem.json"))
    mem.learn(HASH_LOCAL, "sarah@acme.com", ["prefers async updates"])
    pipe = Pipeline(settings=bootable_settings, drafting=CapturingDrafting(),
                    store=InMemoryStateStore(bootable_settings.state_ttl_seconds), memory=mem)
    email = InboundEmail(event_id="rcv2", secret_address_local=HASH_LOCAL,
                         from_address="ceo@example.com", dkim_verified=True, subject="Fwd",
                         body="RE" + FORWARDED_THREAD)
    pipe.handle(email)
    assert "prefers async updates" in seen["ctx"] and "UNCONFIRMED" in seen["ctx"]
    assert seen["autolearn"] is True


def test_layerc_v2_learns_derived_facts_on_r1(bootable_settings, tmp_path) -> None:
    from app.memory import FileMemoryStore
    from app.models import InboundEmail, NoraResult, ResultKind
    from app.pipeline import Pipeline
    from app.state.store import InMemoryStateStore

    class DerivingDrafting:
        def produce(self, req, intent):
            return NoraResult(kind=ResultKind.DRAFT, text="DRAFTED", interpretation_readback="",
                              derived_facts=["prefers mornings"])

    bootable_settings.relationship_autolearn = True
    mem = FileMemoryStore(str(tmp_path / "mem.json"))
    pipe = Pipeline(settings=bootable_settings, drafting=DerivingDrafting(),
                    store=InMemoryStateStore(bootable_settings.state_ttl_seconds), memory=mem)
    email = InboundEmail(event_id="lrn1", secret_address_local=HASH_LOCAL,
                         from_address="ceo@example.com", dkim_verified=True, subject="Fwd",
                         body="RE" + FORWARDED_THREAD)
    pipe.handle(email)
    assert mem.recall(HASH_LOCAL, "sarah@acme.com").derived == ["prefers mornings"]


def test_layerc_v2_off_does_not_learn(bootable_settings, tmp_path) -> None:
    from app.memory import FileMemoryStore
    from app.models import InboundEmail, NoraResult, ResultKind
    from app.pipeline import Pipeline
    from app.state.store import InMemoryStateStore

    seen: dict = {}

    class DerivingDrafting:
        def produce(self, req, intent):
            seen["autolearn"] = req.autolearn
            return NoraResult(kind=ResultKind.DRAFT, text="DRAFTED", interpretation_readback="",
                              derived_facts=["prefers mornings"])

    # flag defaults off
    mem = FileMemoryStore(str(tmp_path / "mem.json"))
    pipe = Pipeline(settings=bootable_settings, drafting=DerivingDrafting(),
                    store=InMemoryStateStore(bootable_settings.state_ttl_seconds), memory=mem)
    email = InboundEmail(event_id="lrn2", secret_address_local=HASH_LOCAL,
                         from_address="ceo@example.com", dkim_verified=True, subject="Fwd",
                         body="RE" + FORWARDED_THREAD)
    pipe.handle(email)
    assert seen["autolearn"] is False
    rec = mem.recall(HASH_LOCAL, "sarah@acme.com")
    assert rec is None or rec.derived == []


def test_autodraft_reply_drafts_for_r1_with_voice_and_memory(bootable_settings, tmp_path) -> None:
    from app.memory import FileMemoryStore
    from app.models import NoraResult, ResultKind
    from app.pipeline import Pipeline
    from app.state.store import InMemoryStateStore
    from app.voice_memory import FileVoiceStore

    seen: dict = {}

    class CapDraft:
        def produce(self, req, intent):
            seen.update(intent=intent, sender=req.thread.original_sender,
                        voice=req.voice_profile, ctx=req.relationship_context)
            return NoraResult(kind=ResultKind.DRAFT, text="Hi Sarah,", interpretation_readback="")

    mem = FileMemoryStore(str(tmp_path / "m.json"))
    mem.remember(HASH_LOCAL, "sarah@acme.com", "prefers mornings")
    voice = FileVoiceStore(str(tmp_path / "v.json"))
    voice.set_profile(HASH_LOCAL, "Preferred style: formal.", counterparty="sarah@acme.com")
    pipe = Pipeline(settings=bootable_settings, drafting=CapDraft(),
                    store=InMemoryStateStore(bootable_settings.state_ttl_seconds),
                    memory=mem, voice=voice)
    result = pipe.autodraft_reply(HASH_LOCAL, "sarah@acme.com", "Proposal", "Can we meet?")
    assert result is not None and result.kind is ResultKind.DRAFT
    assert seen["sender"] == "sarah@acme.com" and seen["intent"].name == "DRAFT"
    assert seen["voice"] == "Preferred style: formal."  # per-recipient profile applied
    assert "prefers mornings" in seen["ctx"]
    assert mem.recall(HASH_LOCAL, "sarah@acme.com").thread_count >= 1  # breadcrumb recorded


def test_autodraft_reply_skips_non_r1_sender(bootable_settings) -> None:
    from app.pipeline import Pipeline
    from app.state.store import InMemoryStateStore

    bootable_settings.posture_map = {"vip@bank.com": "R3"}

    class Boom:
        def produce(self, req, intent):
            raise AssertionError("must not draft for a non-R1 sender")

    pipe = Pipeline(settings=bootable_settings, drafting=Boom(),
                    store=InMemoryStateStore(bootable_settings.state_ttl_seconds))
    assert pipe.autodraft_reply(HASH_LOCAL, "vip@bank.com", "x", "y") is None


def test_layerc_v2_end_to_end_learn_recall_forget(bootable_settings, tmp_path) -> None:
    from app.memory import FileMemoryStore
    from app.models import InboundEmail, NoraResult, ResultKind
    from app.pipeline import Pipeline
    from app.state.store import InMemoryStateStore

    seen: dict = {}

    class DerivingDrafting:
        def produce(self, req, intent):
            seen["ctx"] = req.relationship_context
            return NoraResult(kind=ResultKind.DRAFT, text="DRAFTED", interpretation_readback="",
                              derived_facts=["prefers mornings"])

    bootable_settings.relationship_autolearn = True
    mem = FileMemoryStore(str(tmp_path / "mem.json"))
    pipe = Pipeline(settings=bootable_settings, drafting=DerivingDrafting(),
                    store=InMemoryStateStore(bootable_settings.state_ttl_seconds), memory=mem)

    def _send(eid, body):
        return pipe.handle(InboundEmail(event_id=eid, secret_address_local=HASH_LOCAL,
                                        from_address="ceo@example.com", dkim_verified=True,
                                        subject="Fwd", body=body))[0]

    # Turn 1: derives + stores + surfaces the Learned line.
    reply1 = _send("e2e1", "RE" + FORWARDED_THREAD)
    assert mem.recall(HASH_LOCAL, "sarah@acme.com").derived == ["prefers mornings"]
    assert "Learned: prefers mornings (reply FORGET to drop)" in reply1.body

    # Turn 2: recalls the derived fact into the prompt; nothing new ⇒ no Learned line.
    reply2 = _send("e2e2", "RE" + FORWARDED_THREAD)
    assert "prefers mornings" in seen["ctx"] and "UNCONFIRMED" in seen["ctx"]
    assert "Learned:" not in reply2.body

    # Turn 3: [forget] wipes the derived fact.
    _send("e2e3", "RE [forget]" + FORWARDED_THREAD)
    assert mem.recall(HASH_LOCAL, "sarah@acme.com").derived == []


def _rm_pipe(bootable_settings, tmp_path):
    from app.memory import FileMemoryStore
    from app.pipeline import Pipeline
    from app.state.store import InMemoryStateStore
    from tests.conftest import StubDrafting

    mem = FileMemoryStore(str(tmp_path / "mem.json"))
    pipe = Pipeline(settings=bootable_settings, drafting=StubDrafting(),
                    store=InMemoryStateStore(bootable_settings.state_ttl_seconds), memory=mem)
    return pipe, mem


def _rm_send(pipe, eid, body):
    from app.models import InboundEmail
    return pipe.handle(InboundEmail(event_id=eid, secret_address_local=HASH_LOCAL,
                                    from_address="ceo@example.com", dkim_verified=True,
                                    subject="Fwd", body=body))


def test_rm_code_routes_to_relationship_memory_intent() -> None:
    from app.interpreter import route
    from app.models import Intent, ParsedRequest
    intent, _ = route(ParsedRequest(action_code="RM"))
    assert intent is Intent.RELATIONSHIP_MEMORY


def test_rm_reads_back_curated_and_derived(bootable_settings, tmp_path) -> None:
    pipe, mem = _rm_pipe(bootable_settings, tmp_path)
    mem.remember(HASH_LOCAL, "sarah@acme.com", "prefers morning meetings")
    mem.learn(HASH_LOCAL, "sarah@acme.com", ["is the Brightwave lead"])
    reply, reply_to, action = _rm_send(pipe, "rm1", "RM" + FORWARDED_THREAD)
    body = reply.body
    assert "sarah@acme.com" in body
    assert "prefers morning meetings" in body and "is the Brightwave lead" in body
    assert "Confirmed" in body and "Observed" in body
    assert reply_to is None and action is None  # read-back only, never sends


def test_rm_omits_observed_section_when_no_derived(bootable_settings, tmp_path) -> None:
    pipe, mem = _rm_pipe(bootable_settings, tmp_path)
    mem.remember(HASH_LOCAL, "sarah@acme.com", "prefers mornings")
    reply, _, _ = _rm_send(pipe, "rm2", "RM" + FORWARDED_THREAD)
    assert "prefers mornings" in reply.body and "Observed" not in reply.body


def test_rm_with_nothing_saved(bootable_settings, tmp_path) -> None:
    pipe, _ = _rm_pipe(bootable_settings, tmp_path)
    reply, _, _ = _rm_send(pipe, "rm3", "RM" + FORWARDED_THREAD)
    assert "don't have any saved facts" in reply.body


def test_rm_without_counterparty_asks_to_forward(bootable_settings, tmp_path) -> None:
    pipe, _ = _rm_pipe(bootable_settings, tmp_path)
    reply, _, _ = _rm_send(pipe, "rm4", "RM")  # no forwarded thread → no sender
    assert "forward a thread" in reply.body.lower()


def test_rm_forget_clears_curated_and_derived(bootable_settings, tmp_path) -> None:
    pipe, mem = _rm_pipe(bootable_settings, tmp_path)
    mem.remember(HASH_LOCAL, "sarah@acme.com", "curated")
    mem.learn(HASH_LOCAL, "sarah@acme.com", ["derived"])
    # FORGET is multi-token, so it needs the NB- prefix (same rule as NB-VP FORGET).
    reply, _, _ = _rm_send(pipe, "rm5", "NB-RM FORGET" + FORWARDED_THREAD)
    assert "cleared" in reply.body.lower()
    rec = mem.recall(HASH_LOCAL, "sarah@acme.com")
    assert rec.notes == [] and rec.derived == []


def test_per_recipient_voice_records_both_keys_and_recalls_scoped(bootable_settings, tmp_path) -> None:
    from app.models import InboundEmail, NoraResult, ResultKind
    from app.pipeline import Pipeline
    from app.state.store import InMemoryStateStore
    from app.voice_memory import FileVoiceStore

    seen: dict = {}

    class CapturingDrafting:
        def produce(self, req, intent):
            seen["voice_profile"] = req.voice_profile
            return NoraResult(kind=ResultKind.DRAFT, text="D", interpretation_readback="")

    voice = FileVoiceStore(str(tmp_path / "v.json"))
    pipe = Pipeline(settings=bootable_settings, drafting=CapturingDrafting(),
                    store=InMemoryStateStore(bootable_settings.state_ttl_seconds), voice=voice)
    # A draft to sarah@acme.com carrying a formal refinement records under global + per-cp.
    pipe.handle(InboundEmail(event_id="pv1", secret_address_local=HASH_LOCAL,
                             from_address="ceo@example.com", dkim_verified=True, subject="Fwd",
                             body="NB-RE-F" + FORWARDED_THREAD))
    assert [s["code"] for s in voice.signals(HASH_LOCAL)] == ["F"]
    assert [s["code"] for s in voice.signals(HASH_LOCAL, "sarah@acme.com")] == ["F"]

    # A per-recipient profile is preferred over the global one on the next draft.
    voice.set_profile(HASH_LOCAL, "Preferred style: detailed.")  # global
    voice.set_profile(HASH_LOCAL, "Preferred style: formal.", counterparty="sarah@acme.com")
    pipe.handle(InboundEmail(event_id="pv2", secret_address_local=HASH_LOCAL,
                             from_address="ceo@example.com", dkim_verified=True, subject="Fwd",
                             body="RE" + FORWARDED_THREAD))
    assert seen["voice_profile"] == "Preferred style: formal."


def test_layerc_forget_clears_facts_on_r1(bootable_settings, tmp_path) -> None:
    from app.memory import FileMemoryStore
    from app.models import InboundEmail
    from app.pipeline import Pipeline
    from app.state.store import InMemoryStateStore
    from tests.conftest import StubDrafting

    mem = FileMemoryStore(str(tmp_path / "mem.json"))
    mem.remember(HASH_LOCAL, "sarah@acme.com", "prefers mornings")
    pipe = Pipeline(settings=bootable_settings, drafting=StubDrafting(),
                    store=InMemoryStateStore(bootable_settings.state_ttl_seconds), memory=mem)
    email = InboundEmail(event_id="rc3", secret_address_local=HASH_LOCAL,
                         from_address="ceo@example.com", dkim_verified=True, subject="Fwd",
                         body="RE [forget]" + FORWARDED_THREAD)
    pipe.handle(email)
    assert mem.recall(HASH_LOCAL, "sarah@acme.com").notes == []


def test_send_confirm_writes_outbox_before_consuming_action(fake_provider) -> None:
    # Fix #1: the durable outbox record must exist BEFORE the proposal is marked consumed, so a
    # crash in that window can't lose an approved send (it replays from the outbox).
    captured: dict = {}
    box = InMemorySendOutbox()
    deps = _deps_with_outbox(box, fake_provider)
    actions = deps.pipeline.actions
    orig_record = box.record

    def spy_record(entry):
        rec = actions.get(entry.token)
        captured["status_at_record"] = rec.status if rec else None
        return orig_record(entry)

    box.record = spy_record
    client = TestClient(create_app(_settings(), deps=deps))
    _post(client, MessageID="pm-sm", TextBody="SM" + FORWARDED_THREAD)
    token = fake_provider.sent[-1]["reply_to"].split("+", 1)[1].split("@", 1)[0]
    _post(client, MessageID="pm-ag", MailboxHash=token, TextBody="AG")
    assert captured["status_at_record"] == "pending"      # written while still pending
    assert actions.get(token).status == "consumed"        # single-use still enforced after


def test_nl_request_routes_via_interpreter(fake_deps, fake_provider) -> None:
    from app.nl_interpreter import InterpretedRequest

    class StubInterp:
        def interpret(self, text):
            return InterpretedRequest(intent="summarize")

    fake_deps.pipeline.interpreter = StubInterp()
    client = TestClient(create_app(_settings(), deps=fake_deps))
    _post(client, MessageID="pm-nl", TextBody="what's this about?" + FORWARDED_THREAD)
    assert "SUMMARY" in fake_provider.sent[-1]["body"]


def test_explicit_code_skips_interpreter(fake_deps, fake_provider) -> None:
    calls = {"n": 0}

    class SpyInterp:
        def interpret(self, text):
            calls["n"] += 1
            from app.nl_interpreter import InterpretedRequest
            return InterpretedRequest()

    fake_deps.pipeline.interpreter = SpyInterp()
    client = TestClient(create_app(_settings(), deps=fake_deps))
    _post(client, MessageID="pm-code", TextBody="WIYOW" + FORWARDED_THREAD)
    assert calls["n"] == 0


def test_nl_unclear_asks_the_interpreter_question(fake_deps, fake_provider) -> None:
    from app.nl_interpreter import InterpretedRequest

    class StubInterp:
        def interpret(self, text):
            return InterpretedRequest(intent="unclear", clarify="Reply, summarize, or analyze?")

    fake_deps.pipeline.interpreter = StubInterp()
    client = TestClient(create_app(_settings(), deps=fake_deps))
    _post(client, MessageID="pm-unclear", TextBody="hmm" + FORWARDED_THREAD)
    assert "Reply, summarize, or analyze?" in fake_provider.sent[-1]["body"]


def test_file_disambiguation_readback_is_specific(fake_deps, fake_provider) -> None:
    # The disambiguation clarify should read back what actually happened (several file
    # matches), not the generic "I couldn't tell what you wanted" (showcase polish).
    class _Multi:
        def search(self, ref):
            return ["Q3-2026-earnings.txt", "Q4-2026-earnings.docx"]

        def read(self, ref):
            return "SHOULD NOT BE READ"

    fake_deps.pipeline.file_source = _Multi()
    client = TestClient(create_app(_settings(), deps=fake_deps))
    _post(client, MessageID="pm-amb-readback", TextBody="S [file: earnings]" + FORWARDED_THREAD)
    body = fake_provider.sent[-1]["body"]
    assert "couldn't tell what you wanted" not in body
    assert "several files match" in body.lower()
