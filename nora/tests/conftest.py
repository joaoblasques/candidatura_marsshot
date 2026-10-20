"""Shared test fixtures."""

from __future__ import annotations

import base64

import pytest

from app.config import Settings
from app.email.outbound import OutboundSender
from app.main import Dependencies
from app.models import Intent, NoraResult, ParsedRequest, ResultKind
from app.pipeline import Pipeline
from app.state.store import InMemoryStateStore

# Webhook Basic-Auth credential used across the inbound/route tests.
WEBHOOK_CRED = "hook-user:hook-pass"
# The Postmark inbound server-hash local part the dogfood CEO is registered under.
HASH_LOCAL = "abc123hash"
# Canonical forwarded-thread sample appended after a command code in test bodies.
FORWARDED_THREAD = (
    "\n\n---------- Forwarded message ----------\n"
    "From: Sarah <sarah@acme.com>\nTo: CEO <ceo@example.com>\n"
    "Subject: Proposal\n\nHi.\n"
)


def basic_auth_header(credential: str = WEBHOOK_CRED) -> dict[str, str]:
    """Build an HTTP Basic ``Authorization`` header for the webhook."""
    encoded = base64.b64encode(credential.encode()).decode()
    return {"Authorization": f"Basic {encoded}"}


def postmark_payload(**overrides) -> dict:
    """A Postmark inbound-parse webhook payload (DKIM-aligned by default)."""
    base = {
        "MessageID": "pm-evt-1",
        "OriginalRecipient": f"{HASH_LOCAL}@inbound.postmarkapp.com",
        "FromFull": {"Email": "ceo@example.com", "Name": "CEO"},
        "From": "ceo@example.com",
        "Subject": "Fwd: Proposal",
        "TextBody": "WIYOW" + FORWARDED_THREAD,
        "MailboxHash": "",
        "Headers": [
            {
                "Name": "Authentication-Results",
                "Value": "mx.cloudflare.net; dkim=pass header.d=example.com; spf=pass; dmarc=pass",
            }
        ],
        "Attachments": [],
    }
    base.update(overrides)
    return base


@pytest.fixture(autouse=True)
def _hermetic_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests must not read the developer's local .env (it holds live config like
    NORA_ADDRESS_DOMAIN / real credentials), which would override test settings."""
    monkeypatch.setitem(Settings.model_config, "env_file", None)


@pytest.fixture
def bootable_settings() -> Settings:
    """A minimal settings object that passes the boot gate."""
    return Settings(
        external_llm_ack=True,
        anthropic_api_key="test-anthropic-key",
        provider_api_key="test-provider-key",
        webhook_basic_auth=WEBHOOK_CRED,
        postmark_sender="nora@example.com",
        ceo_registry={HASH_LOCAL: "ceo@example.com"},
    )


class StubDrafting:
    """A drafting service that returns canned output without calling Claude."""

    def produce(self, req: ParsedRequest, intent: Intent) -> NoraResult:
        if intent is Intent.DRAFT:
            return NoraResult(
                kind=ResultKind.DRAFT,
                text=f"DRAFTED reply re: {req.thread.subject or 'thread'}",
                interpretation_readback="Draft a reply.",
                recipient=req.thread.original_sender,
                recipient_uncertain=req.recipient_uncertain,
            )
        if intent is Intent.ANALYZE:
            return NoraResult(
                kind=ResultKind.ANALYSIS,
                text="ANALYSIS of the thread.",
                interpretation_readback="Analyze the thread.",
            )
        if intent is Intent.REPORT:
            return NoraResult(
                kind=ResultKind.REPORT,
                text="REPORT on the thread.",
                interpretation_readback="Prepare a report.",
            )
        if intent is Intent.PROPOSE_MEETING:
            return NoraResult(
                kind=ResultKind.PROPOSAL,
                text=f"Hi — I'm Nora, EA to the CEO, writing on his behalf to propose a "
                f"meeting re: {req.thread.subject or 'thread'}. Best, Nora",
                interpretation_readback="Propose a meeting.",
                recipient=req.thread.original_sender,
                recipient_uncertain=req.recipient_uncertain,
            )
        return NoraResult(
            kind=ResultKind.SUMMARY,
            text="SUMMARY of the thread.",
            interpretation_readback="Summarize the thread.",
        )


class FakeProviderClient:
    """Captures outbound sends instead of calling a provider."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    def send(
        self,
        *,
        to: str,
        subject: str,
        body: str,
        reply_to: str | None,
        sender: str | None = None,
        idempotency_key: str | None = None,
        attachments: list | None = None,
    ) -> None:
        self.sent.append(
            {
                "to": to,
                "subject": subject,
                "body": body,
                "reply_to": reply_to,
                "sender": sender,
                "idempotency_key": idempotency_key,
                "attachments": attachments,
            }
        )


@pytest.fixture
def fake_provider() -> FakeProviderClient:
    return FakeProviderClient()


@pytest.fixture
def fake_deps(bootable_settings: Settings, fake_provider: FakeProviderClient) -> Dependencies:
    """Dependencies wired with a stub drafting service and a capturing sender."""
    store = InMemoryStateStore(bootable_settings.state_ttl_seconds)
    pipeline = Pipeline(settings=bootable_settings, drafting=StubDrafting(), store=store)
    return Dependencies(pipeline=pipeline, outbound=OutboundSender(fake_provider))
