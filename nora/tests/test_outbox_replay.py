"""Boot-replay of a stranded consent-gated send must not double-send on a
non-idempotent transport (audit finding #6)."""

from __future__ import annotations

from app.email.outbound import OutboundSender
from app.main import Dependencies, _replay_outbox
from app.state.outbox import InMemorySendOutbox, OutboxRecord

from tests.conftest import FakeProviderClient


def _record() -> OutboxRecord:
    return OutboxRecord(
        token="t1", recipient="sarah@acme.com", subject="Proposal", body="...",
        ceo_address="ceo@x.com", ceo_id="nora", event_id="e1",
    )


def _deps(client) -> Dependencies:
    outbox = InMemorySendOutbox()
    outbox.record(_record())
    return Dependencies(pipeline=None, outbound=OutboundSender(client), outbox=outbox)  # type: ignore[arg-type]


def test_non_idempotent_transport_does_not_resend_and_clears(bootable_settings) -> None:
    client = FakeProviderClient()  # supports_idempotency defaults False
    deps = _deps(client)
    _replay_outbox(deps, bootable_settings)
    # No third-party resend...
    assert all(s["to"] != "sarah@acme.com" for s in client.sent)
    # ...but the CEO is told to check, and the record is cleared so it can't replay.
    assert any(s["to"] == "ceo@x.com" for s in client.sent)
    assert deps.outbox.pending() == []


def test_idempotent_transport_replays_the_send(bootable_settings) -> None:
    client = FakeProviderClient()
    client.supports_idempotency = True  # emulate Resend
    deps = _deps(client)
    _replay_outbox(deps, bootable_settings)
    # The third-party send is completed (idempotency key makes replay safe).
    assert any(s["to"] == "sarah@acme.com" for s in client.sent)
    assert deps.outbox.pending() == []


def test_share_outbox_record_round_trips_through_file_store(tmp_path) -> None:
    # file_ref + share_mode must survive JSON serialization in the durable outbox.
    from app.state.outbox import FileSendOutbox
    path = str(tmp_path / "outbox.json")
    FileSendOutbox(path).record(OutboxRecord(
        token="t", recipient="s@x.com", subject="S", body="b",
        ceo_address="ceo@x.com", ceo_id="nora", event_id="e",
        file_ref="deck.pdf", share_mode="grant",
    ))
    [r] = FileSendOutbox(path).pending()
    assert r.file_ref == "deck.pdf" and r.share_mode == "grant"


def test_boot_replay_of_stranded_share_grant_grants_and_emails(bootable_settings) -> None:
    # A stranded share-grant record on an idempotent transport: replay grants access and
    # emails the link (the file is re-fetched/granted fresh at replay — never persisted).
    class _Granter:
        def grant(self, ref, recipient):
            return f"https://store/{ref}"
        def fetch(self, ref):
            return None
        def read(self, ref):
            return None
        def search(self, ref):
            return [ref]

    client = FakeProviderClient()
    client.supports_idempotency = True
    outbox = InMemorySendOutbox()
    outbox.record(OutboxRecord(
        token="t1", recipient="sarah@acme.com", subject="S", body="see link",
        ceo_address="ceo@x.com", ceo_id="nora", event_id="e",
        file_ref="deck.pdf", share_mode="grant",
    ))
    deps = Dependencies(pipeline=None, outbound=OutboundSender(client),  # type: ignore[arg-type]
                        outbox=outbox, file_source=_Granter())
    _replay_outbox(deps, bootable_settings)
    shared = [s for s in client.sent if s["to"] == "sarah@acme.com"]
    assert shared and "https://store/deck.pdf" in shared[0]["body"]
    assert outbox.pending() == []
