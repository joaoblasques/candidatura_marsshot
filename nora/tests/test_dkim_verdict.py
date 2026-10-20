"""U3 — real DKIM verdict parsing (Authentication-Results) feeding dkim_verified.

Hermetic: no real mail, no network. Drives the public adapter
``adapt_postmark_inbound`` with crafted payloads and asserts the resulting
``InboundEmail.dkim_verified``.
"""

from __future__ import annotations

from app.email.postmark_inbound import adapt_postmark_inbound


def _payload(*, from_email: str, headers: list[dict]) -> dict:
    return {
        "MessageID": "evt-1",
        "OriginalRecipient": "nora@nora-bennett.com",
        "FromFull": {"Email": from_email},
        "From": from_email,
        "Subject": "Fwd: x",
        "TextBody": "WIYOW",
        "MailboxHash": "",
        "Headers": headers,
        "Attachments": [],
    }


def _ar(value: str) -> list[dict]:
    return [{"Name": "Authentication-Results", "Value": value}]


def _verdict(from_email: str, ar: str) -> bool:
    return adapt_postmark_inbound(_payload(from_email=from_email, headers=_ar(ar))).dkim_verified


# --- happy path -------------------------------------------------------------

def test_dkim_pass_aligned_header_d() -> None:
    assert _verdict(
        "ceo@example.com",
        "mx.cloudflare.net; dkim=pass header.d=example.com header.s=s1; spf=pass; dmarc=pass",
    ) is True


def test_dkim_pass_aligned_subdomain() -> None:
    # signing domain is a parent of the From domain (relaxed alignment)
    assert _verdict(
        "ceo@mail.example.com",
        "mx.cloudflare.net; dkim=pass header.d=example.com; dmarc=pass",
    ) is True


def test_dkim_pass_header_i_form() -> None:
    assert _verdict(
        "ceo@example.com",
        "mx.cloudflare.net; dkim=pass header.i=@example.com; spf=pass",
    ) is True


def test_multiple_dkim_results_one_aligned_pass() -> None:
    assert _verdict(
        "ceo@example.com",
        "mx; dkim=pass header.d=other.com; dkim=pass header.d=example.com; dmarc=pass",
    ) is True


# --- fail-closed cases ------------------------------------------------------

def test_dkim_pass_but_unaligned_domain_is_rejected() -> None:
    # the spoof-via-AR case: a valid signature, but for a domain != the From author
    assert _verdict(
        "ceo@example.com",
        "mx.cloudflare.net; dkim=pass header.d=attacker.com; spf=pass; dmarc=fail",
    ) is False


def test_dkim_fail_is_rejected() -> None:
    assert _verdict("ceo@example.com", "mx; dkim=fail header.d=example.com") is False


def test_dkim_none_is_rejected() -> None:
    assert _verdict("ceo@example.com", "mx; dkim=none; spf=pass") is False


def test_missing_authentication_results_is_rejected() -> None:
    email = adapt_postmark_inbound(_payload(from_email="ceo@example.com", headers=[]))
    assert email.dkim_verified is False


def test_malformed_authentication_results_is_rejected() -> None:
    assert _verdict("ceo@example.com", "garbage;;; not a real header @@@") is False


def test_unparseable_from_never_aligns() -> None:
    assert _verdict("not-an-email", "mx; dkim=pass header.d=example.com") is False


# --- X-Spam-Tests is no longer a verdict source (#4) ------------------------
# The Worker now stamps Authentication-Results with the trusted authserv-id, so
# the bare DKIM_VALID_AU substring fallback (no authserv-id binding) is removed.

def test_xspam_tests_dkim_valid_au_no_longer_authenticates() -> None:
    email = adapt_postmark_inbound(
        _payload(
            from_email="ceo@example.com",
            headers=[{"Name": "X-Spam-Tests", "Value": "DKIM_SIGNED,DKIM_VALID_AU,SPF_PASS"}],
        )
    )
    assert email.dkim_verified is False


# --- alignment is public-suffix-aware (#5) ----------------------------------

def test_bare_public_suffix_author_does_not_align_arbitrary_signer() -> None:
    # author domain is a bare TLD; a *.com signer must not author-align to it
    assert _verdict("ceo@com", "mx; dkim=pass header.d=attacker.com; spf=pass") is False


def test_bare_public_suffix_signer_does_not_align_arbitrary_author() -> None:
    assert _verdict("ceo@attacker.com", "mx; dkim=pass header.d=com; spf=pass") is False
