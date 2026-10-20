"""U1/U4: configuration parsing, the boot gate, and log redaction."""

from __future__ import annotations

import pytest

from app.config import Settings
from app.logging import redact


def _complete(**over) -> Settings:
    base = dict(
        external_llm_ack=True,
        anthropic_api_key="k",
        provider_api_key="k",
        webhook_basic_auth="user:pass",
        postmark_sender="nora@example.com",
    )
    base.update(over)
    return Settings(**base)


def test_ceo_registry_parses_json_string() -> None:
    settings = _complete(ceo_registry='{"abc123hash": "ceo@example.com"}')
    assert settings.ceo_registry == {"abc123hash": "ceo@example.com"}


def test_require_bootable_passes_when_complete() -> None:
    _complete().require_bootable()  # should not raise


def test_require_bootable_fails_without_webhook_credential() -> None:
    with pytest.raises(RuntimeError, match="NORA_WEBHOOK_BASIC_AUTH"):
        _complete(webhook_basic_auth="").require_bootable()


def test_require_bootable_fails_without_sender() -> None:
    with pytest.raises(RuntimeError, match="NORA_POSTMARK_SENDER"):
        _complete(postmark_sender="").require_bootable()


def test_smtp_transport_boots_without_postmark_token() -> None:
    # The smtp transport (local demo / Gmail) needs no Postmark server token.
    _complete(transport="smtp", smtp_host="localhost", provider_api_key="").require_bootable()


def test_smtp_transport_requires_smtp_host() -> None:
    with pytest.raises(RuntimeError, match="NORA_SMTP_HOST"):
        _complete(transport="smtp", smtp_host="", provider_api_key="").require_bootable()


def test_unknown_transport_rejected() -> None:
    with pytest.raises(RuntimeError, match="Unknown NORA_TRANSPORT"):
        _complete(transport="carrier-pigeon").require_bootable()


def test_gmail_oauth_boots_without_service_account_key() -> None:
    # Personal-Gmail OAuth mode requires the OAuth client, not a service-account key.
    _complete(transport="gmail", gmail_auth="oauth", gmail_oauth_client="/c.json",
              gmail_tenant_user="me@gmail.com", provider_api_key="").require_bootable()


def test_gmail_oauth_requires_oauth_client() -> None:
    with pytest.raises(RuntimeError, match="NORA_GMAIL_OAUTH_CLIENT"):
        _complete(transport="gmail", gmail_auth="oauth", gmail_oauth_client="",
                  gmail_tenant_user="me@gmail.com", provider_api_key="").require_bootable()


def test_resend_transport_boots_with_api_key() -> None:
    _complete(transport="resend", resend_api_key="re_x", provider_api_key="").require_bootable()


def test_resend_transport_requires_api_key() -> None:
    with pytest.raises(RuntimeError, match="NORA_RESEND_API_KEY"):
        _complete(transport="resend", resend_api_key="", provider_api_key="").require_bootable()


def test_ollama_backend_rejected_with_prod_transport() -> None:
    import pytest
    with pytest.raises(RuntimeError):
        _complete(drafting_backend="ollama", transport="resend", resend_api_key="r").require_bootable()


def test_ollama_backend_ok_with_dev_transport() -> None:
    _complete(drafting_backend="ollama", transport="smtp", smtp_host="h").require_bootable()


def test_unknown_local_drafting_backend_rejected() -> None:
    # A typo must fail loud at boot, not silently disable KL/local handling.
    with pytest.raises(RuntimeError, match="Unknown NORA_LOCAL_DRAFTING_BACKEND"):
        _complete(local_drafting_backend="olama").require_bootable()


def test_local_drafting_backend_ollama_with_prod_transport_ok() -> None:
    # local_drafting_backend is SEPARATE from drafting_backend: R3/KL runs on-device while
    # R1 stays on Claude, so 'ollama' here is valid with a production transport.
    _complete(local_drafting_backend="ollama", transport="resend", resend_api_key="r",
              provider_api_key="").require_bootable()


def test_default_anthropic_backend_unaffected() -> None:
    _complete().require_bootable()  # no raise


def test_redact_hides_sensitive_fields() -> None:
    record = {
        "event_id": "evt_1",
        "from_address": "ceo@example.com",
        "subject": "Re: Proposal",
        "body": "secret thread content",
        "recipients": ["sarah@acme.com"],
        "ceo_registry": {"abc123hash": "ceo@example.com"},
        "secret_address_local": "abc123hash",
    }
    out = redact(record)
    assert out["event_id"] == "evt_1"
    assert out["from_address"] == "<redacted>"
    assert out["subject"] == "<redacted>"
    assert out["body"] == "<redacted>"
    assert out["recipients"] == "<redacted>"
    assert out["ceo_registry"] == "<redacted>"
    assert out["secret_address_local"] == "<redacted>"


def test_unknown_tenant_backend_fails_boot() -> None:
    with pytest.raises(RuntimeError, match="NORA_TENANT_BACKEND"):
        _complete(tenant_backend="dropbox").require_bootable()


def test_gmail_tenant_backend_requires_creds_and_user() -> None:
    with pytest.raises(RuntimeError, match="NORA_GMAIL_CREDENTIALS_JSON"):
        _complete(tenant_backend="gmail", gmail_tenant_user="ceo@client.com").require_bootable()
    with pytest.raises(RuntimeError, match="NORA_GMAIL_TENANT_USER"):
        _complete(tenant_backend="gmail", gmail_credentials_json="/k.json").require_bootable()
    # complete → boots
    _complete(tenant_backend="gmail", gmail_credentials_json="/k.json",
              gmail_tenant_user="ceo@client.com").require_bootable()


def test_unknown_calendar_and_research_backends_fail_boot() -> None:
    with pytest.raises(RuntimeError, match="NORA_CALENDAR_BACKEND"):
        _complete(calendar_backend="outlook").require_bootable()
    with pytest.raises(RuntimeError, match="NORA_RESEARCH_BACKEND"):
        _complete(research_backend="bing").require_bootable()


def test_google_calendar_requires_credentials() -> None:
    with pytest.raises(RuntimeError, match="NORA_GOOGLE_CREDENTIALS_JSON"):
        _complete(calendar_backend="google").require_bootable()


def test_graph_tenant_backend_requires_all_four_settings() -> None:
    with pytest.raises(RuntimeError, match="NORA_GRAPH_"):
        _complete(tenant_backend="graph").require_bootable()
    # complete → boots
    _complete(tenant_backend="graph", graph_tenant_id="t", graph_client_id="c",
              graph_client_secret="s", graph_tenant_user="ceo@client.com").require_bootable()


def test_graph_file_source_requires_graph_settings() -> None:
    with pytest.raises(RuntimeError, match="NORA_GRAPH_"):
        _complete(file_source_backend="graph").require_bootable()
    _complete(file_source_backend="graph", graph_tenant_id="t", graph_client_id="c",
              graph_client_secret="s", graph_tenant_user="ceo@client.com").require_bootable()


def test_unknown_file_source_backend_fails_boot() -> None:
    with pytest.raises(RuntimeError, match="NORA_FILE_SOURCE_BACKEND"):
        _complete(file_source_backend="dropbox").require_bootable()


def test_gmail_transport_requires_showcase_creds() -> None:
    with pytest.raises(RuntimeError, match="NORA_GMAIL"):
        _complete(transport="gmail").require_bootable()
    _complete(transport="gmail", gmail_credentials_json="/k.json",
              gmail_tenant_user="nora@demo.nora-bennett.com").require_bootable()


def test_deep_research_backend_requires_key() -> None:
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        _complete(research_backend="deep", anthropic_api_key="").require_bootable()
    _complete(research_backend="deep", anthropic_api_key="sk-x").require_bootable()


def test_followup_retention_defaults() -> None:
    s = _complete()
    assert s.pending_action_ttl_seconds == 432_000  # 5 days
    assert s.followup_remind_after_seconds == 172_800  # 2 days
    assert s.followup_max_reminders == 1
    assert s.state_ttl_seconds == 86_400  # revise records unchanged


def test_voice_memory_defaults_off() -> None:
    s = _complete()
    assert s.voice_memory is False
    assert s.voice_signal_ttl_seconds == 7_776_000 and s.voice_max_signals == 200


def test_relationship_autolearn_defaults_off() -> None:
    s = _complete()
    assert s.relationship_autolearn is False


def test_autodraft_defaults_off() -> None:
    s = _complete()
    assert s.autodraft is False and s.autodraft_label == "Nora/seen"


def test_inbox_actions_defaults_off() -> None:
    s = _complete()
    assert s.inbox_actions is False and s.command_alias == "nora"
    assert s.filed_label == "Nora/filed" and s.inbox_rules_path == ".nora-inbox-rules.json"


def test_draft_learning_defaults_off() -> None:
    s = _complete()
    assert s.draft_learning is False and s.draft_feedback_path == ".nora-draft-feedback.json"


def test_gmail_auth_defaults_to_service_account() -> None:
    s = _complete()
    assert s.gmail_auth == "service_account"
    assert s.gmail_oauth_client == "" and s.gmail_oauth_token == ".nora-gmail-token.json"


def test_gmail_auth_oauth_from_env(monkeypatch) -> None:
    monkeypatch.setenv("NORA_GMAIL_AUTH", "oauth")
    assert _complete().gmail_auth == "oauth"


def test_autodraft_reads_env(monkeypatch) -> None:
    monkeypatch.setenv("NORA_AUTODRAFT", "true")
    assert _complete().autodraft is True


def test_relationship_autolearn_reads_env(monkeypatch) -> None:
    monkeypatch.setenv("NORA_RELATIONSHIP_AUTOLEARN", "true")
    s = _complete()
    assert s.relationship_autolearn is True


def test_onboarding_settings_default_off() -> None:
    s = _complete()
    assert s.signup_local == "signup" and s.signup_allowlist == [] and s.users_path == ".nora-users.json"


def test_signup_allowlist_parses_csv_env() -> None:
    s = _complete(signup_allowlist="@acme.com, vip@globex.com")
    assert s.signup_allowlist == ["@acme.com", "vip@globex.com"]


def test_signup_allowlist_csv_from_env_does_not_crash_boot(monkeypatch) -> None:
    # Regression: a bare CSV value arriving via the env/dotenv source must parse (NoDecode),
    # not raise SettingsError. _complete() doesn't pass signup_allowlist, so the env source fills it.
    monkeypatch.setenv("NORA_SIGNUP_ALLOWLIST", "owner@example.com, @acme.com")
    s = _complete()
    assert s.signup_allowlist == ["owner@example.com", "@acme.com"]


def test_signup_allowlist_json_from_env(monkeypatch) -> None:
    monkeypatch.setenv("NORA_SIGNUP_ALLOWLIST", '["@globex.com"]')
    assert _complete().signup_allowlist == ["@globex.com"]


def test_signup_local_colliding_with_registry_fails_boot() -> None:
    s = _complete(signup_allowlist="@acme.com", ceo_registry={"signup": "boss@x.com"}, signup_local="signup")
    import pytest
    with pytest.raises(RuntimeError, match="collides"):
        s.require_bootable()


def test_nl_interpreter_backend_default_off() -> None:
    assert _complete().nl_interpreter_backend == "keyword"


def test_openrouter_drafting_requires_key() -> None:
    # gmail transport so the non-anthropic guard allows the backend; key still required.
    with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
        _complete(transport="smtp", smtp_host="localhost", drafting_backend="openrouter",
                  openrouter_api_key="").require_bootable()


def test_openrouter_backend_boots_with_key() -> None:
    _complete(transport="smtp", smtp_host="localhost", drafting_backend="openrouter",
              openrouter_api_key="sk-or").require_bootable()  # should not raise


def test_openrouter_interpreter_requires_key() -> None:
    with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
        _complete(nl_interpreter_backend="openrouter", openrouter_api_key="").require_bootable()


def test_unknown_drafting_backend_raises() -> None:
    with pytest.raises(RuntimeError, match="Unknown NORA_DRAFTING_BACKEND"):
        _complete(drafting_backend="opnrouter").require_bootable()


def test_anthropic_key_required_by_default() -> None:
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        _complete(anthropic_api_key="").require_bootable()


def test_boots_anthropic_free_on_openrouter() -> None:
    # Fully OpenRouter (drafting + interpreter), no Claude research → no Anthropic key needed.
    _complete(
        anthropic_api_key="", transport="smtp", smtp_host="localhost",
        drafting_backend="openrouter", nl_interpreter_backend="openrouter",
        openrouter_api_key="sk-or", research_backend="none",
    ).require_bootable()  # should not raise


def test_anthropic_still_required_when_a_claude_feature_is_on() -> None:
    # Drafting on OpenRouter, but the LLM interpreter still uses Claude → key still required.
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        _complete(
            anthropic_api_key="", transport="smtp", smtp_host="localhost",
            drafting_backend="openrouter", openrouter_api_key="sk-or",
            nl_interpreter_backend="llm",
        ).require_bootable()
    # And web research on Claude likewise requires it.
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        _complete(
            anthropic_api_key="", transport="smtp", smtp_host="localhost",
            drafting_backend="openrouter", openrouter_api_key="sk-or",
            research_backend="claude",
        ).require_bootable()
