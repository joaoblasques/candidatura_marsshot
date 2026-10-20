"""U7: revise-loop state store, correlation, retention, and pipeline revise path."""

from __future__ import annotations

import time

from app.config import Settings
from app.models import InboundEmail, ParsedThread, ResultKind
from app.pipeline import Pipeline
from app.state.store import InMemoryStateStore, ReviseRecord, new_correlation_token

from tests.conftest import StubDrafting


def _settings(**over) -> Settings:
    base = dict(
        external_llm_ack=True,
        anthropic_api_key="k",
        provider_api_key="k",
        webhook_basic_auth="u:p",
        postmark_sender="nora@example.com",
        ceo_registry={"nora-abc123": "ceo@example.com"},
        address_domain="nora.ai",
    )
    base.update(over)
    return Settings(**base)


def _pipeline(settings: Settings) -> Pipeline:
    return Pipeline(
        settings=settings,
        drafting=StubDrafting(),
        store=InMemoryStateStore(settings.state_ttl_seconds),
    )


def _forward(body: str, token: str | None = None) -> InboundEmail:
    return InboundEmail(
        event_id="e",
        secret_address_local="nora-abc123",
        from_address="ceo@example.com",
        dkim_verified=True,
        body=body,
        correlation_token=token,
    )


GMAIL_WIYOW = (
    "WIYOW\n\n---------- Forwarded message ----------\n"
    "From: Sarah <sarah@acme.com>\nTo: CEO <ceo@example.com>\nSubject: Proposal\n\nHi.\n"
)


# --- store ------------------------------------------------------------------


def test_store_put_get_delete() -> None:
    store = InMemoryStateStore(ttl_seconds=100)
    rec = ReviseRecord(
        token="t1", owner_secret_local="nora-abc123", thread=ParsedThread(),
        origin_kind=ResultKind.DRAFT,
    )
    store.put(rec)
    assert store.get("t1") is not None
    store.delete("t1")
    assert store.get("t1") is None


def test_store_expiry_and_purge() -> None:
    store = InMemoryStateStore(ttl_seconds=10)
    rec = ReviseRecord(
        token="t1", owner_secret_local="nora-abc123", thread=ParsedThread(),
        origin_kind=ResultKind.DRAFT, created_at=time.time() - 100,
    )
    store.put(rec)
    assert store.get("t1") is None  # expired on read
    store.put(rec)
    assert store.purge() == 1


# --- pipeline revise path ---------------------------------------------------


def test_new_draft_saves_state_and_returns_reply_to() -> None:
    pipe = _pipeline(_settings())
    reply, reply_to, _ = pipe.handle(_forward(GMAIL_WIYOW))
    assert reply.subject.startswith("Nora · draft reply")
    assert reply_to is not None and reply_to.startswith("nora-abc123+")
    token = reply_to.split("+", 1)[1].split("@", 1)[0]
    assert pipe.store.get(token) is not None


def test_revise_correlates_and_bumps_counter() -> None:
    pipe = _pipeline(_settings())
    _, reply_to, _ = pipe.handle(_forward(GMAIL_WIYOW))
    token = reply_to.split("+", 1)[1].split("@", 1)[0]

    reply2, reply_to2, _ = pipe.handle(_forward("make it warmer", token=token))
    assert "draft reply" in reply2.subject
    assert reply_to2 is not None
    assert pipe.store.get(token).revision_count == 1


def test_revise_rejected_for_wrong_owner() -> None:
    # P0 regression: a different CEO presenting another CEO's token must not
    # fetch their stored thread — it looks identical to an unknown token.
    pipe = _pipeline(_settings())
    _, reply_to, _ = pipe.handle(_forward(GMAIL_WIYOW))  # owned by nora-abc123
    token = reply_to.split("+", 1)[1].split("@", 1)[0]
    other = InboundEmail(
        event_id="e2",
        secret_address_local="nora-other",
        from_address="other@x.com",
        dkim_verified=True,
        body="make it warmer",
        correlation_token=token,
    )
    reply, rt, _ = pipe.handle(other)
    assert reply.subject == "Nora · couldn't find that thread"
    assert rt is None


class _RaisingDrafting:
    def produce(self, req, intent):  # noqa: ANN001, ANN201
        from app.drafting.claude import DraftingError

        raise DraftingError("boom")


def test_drafting_error_yields_graceful_clarify() -> None:
    pipe = Pipeline(
        settings=_settings(),
        drafting=_RaisingDrafting(),
        store=InMemoryStateStore(_settings().state_ttl_seconds),
    )
    reply, reply_to, _ = pipe.handle(_forward(GMAIL_WIYOW))
    assert "couldn't draft that just now" in reply.body
    assert reply_to is None  # no draft record created on failure


def test_lost_thread_when_token_unknown() -> None:
    pipe = _pipeline(_settings())
    reply, reply_to, _ = pipe.handle(_forward("make it warmer", token="doesnotexist"))
    assert reply.subject == "Nora · couldn't find that thread"
    assert reply_to is None


def test_summary_is_terminal_not_revisable() -> None:
    pipe = _pipeline(_settings())
    # Seed a SUMMARY-origin record, then reply to it.
    token = new_correlation_token()
    pipe.store.put(
        ReviseRecord(
            token=token, owner_secret_local="nora-abc123",
            thread=ParsedThread(subject="X"), origin_kind=ResultKind.SUMMARY,
        )
    )
    reply, reply_to, _ = pipe.handle(_forward("make it shorter", token=token))
    assert "nothing to revise" in reply.body.lower()
    assert reply_to is None


def test_revise_cap_stops_offering_revisions() -> None:
    pipe = _pipeline(_settings(max_revise_rounds=1))
    _, reply_to, _ = pipe.handle(_forward(GMAIL_WIYOW))
    token = reply_to.split("+", 1)[1].split("@", 1)[0]
    pipe.handle(_forward("warmer", token=token))  # revision 1 (count -> 1)
    reply, reply_to3, _ = pipe.handle(_forward("warmer again", token=token))  # at cap
    assert "revised this several times" in reply.body
    assert reply_to3 is None
    assert pipe.store.get(token) is None  # cleaned up


def test_first_contact_notice_shown_once() -> None:
    pipe = _pipeline(_settings())
    reply1, _, _ = pipe.handle(_forward(GMAIL_WIYOW))
    assert "First time using Nora" in reply1.body
    reply2, _, _ = pipe.handle(_forward(GMAIL_WIYOW))  # same CEO, fresh forward
    assert "First time using Nora" not in reply2.body


def test_bare_forward_without_intent_clarifies() -> None:
    pipe = _pipeline(_settings())
    body = "---------- Forwarded message ----------\nFrom: Sarah <sarah@acme.com>\n\nHi.\n"
    reply, reply_to, _ = pipe.handle(_forward(body))
    assert reply.subject == "Nora · quick question"
    assert reply_to is None
