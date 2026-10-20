"""U2: sender authentication (secret address + DKIM-verified From)."""

from __future__ import annotations

from app.auth.sender import authenticate
from app.models import InboundEmail

REGISTRY = {"nora-abc123": "ceo@example.com"}


def _email(**overrides) -> InboundEmail:
    base = dict(
        event_id="evt_1",
        secret_address_local="nora-abc123",
        from_address="ceo@example.com",
        dkim_verified=True,
    )
    base.update(overrides)
    return InboundEmail(**base)


def test_authenticates_registered_dkim_aligned_ceo() -> None:
    assert authenticate(_email(), REGISTRY) is True


def test_case_insensitive_from_match() -> None:
    assert authenticate(_email(from_address="CEO@Example.com"), REGISTRY) is True


def test_rejects_dkim_failure() -> None:
    assert authenticate(_email(dkim_verified=False), REGISTRY) is False


def test_rejects_from_mismatch_spoof() -> None:
    assert authenticate(_email(from_address="attacker@evil.com"), REGISTRY) is False


def test_rejects_unknown_secret_address() -> None:
    assert authenticate(_email(secret_address_local="nora-unknown"), REGISTRY) is False


# --- multiple allowed addresses per person ---------------------------------

LIST_REGISTRY = {"democeo": ["a@x.com", "b@y.com", "democeo@mac.com"]}


def _list_email(**overrides) -> InboundEmail:
    base = dict(
        event_id="e",
        secret_address_local="democeo",
        from_address="a@x.com",
        dkim_verified=True,
    )
    base.update(overrides)
    return InboundEmail(**base)


def test_authenticates_any_address_in_list() -> None:
    assert authenticate(_list_email(from_address="b@y.com"), LIST_REGISTRY) is True
    # case-insensitive against a list entry
    assert authenticate(_list_email(from_address="democeo@mac.com"), LIST_REGISTRY) is True


def test_rejects_address_not_in_list() -> None:
    assert authenticate(_list_email(from_address="attacker@evil.com"), LIST_REGISTRY) is False


def test_list_entry_still_requires_dkim() -> None:
    assert authenticate(_list_email(from_address="a@x.com", dkim_verified=False), LIST_REGISTRY) is False


def test_allowed_senders_normalizes() -> None:
    from app.auth.sender import allowed_senders

    assert allowed_senders("A@X.com") == ["a@x.com"]
    assert allowed_senders(["A@X.com", " b@y.com "]) == ["a@x.com", "b@y.com"]
    assert allowed_senders(None) == []
