"""U3: email & command parsing with per-client forwarded-thread fixtures."""

from __future__ import annotations

from app.email.parser import (
    detect_command,
    parse_request,
    split_authored_region,
)
from app.models import Command, InboundEmail

GMAIL = """WIYOW

---------- Forwarded message ----------
From: Sarah Lee <sarah@acme.com>
Date: Mon, 26 May 2026 at 10:00
Subject: Proposal
To: CEO <ceo@example.com>

Hi, here is our proposal, let us know.
"""

APPLE = """S

Begin forwarded message:

From: Sarah Lee <sarah@acme.com>
Subject: Proposal
Date: May 26, 2026 at 10:00:00 AM
To: CEO <ceo@example.com>

Hi, here is our proposal.
"""

OUTLOOK = """reply declining politely

From: Sarah Lee <sarah@acme.com>
Sent: Monday, May 26, 2026 10:00 AM
To: CEO <ceo@example.com>
Subject: Proposal

Hi, here is our proposal.
"""

MULTIPARTY = """WIYOW

---------- Forwarded message ----------
From: Sarah Lee <sarah@acme.com>
To: CEO <ceo@example.com>, Mark Chen <mark@beta.com>
Subject: Re: Proposal

On Mon wrote:
From: Mark Chen <mark@beta.com>
To: Sarah Lee <sarah@acme.com>
"""


def _email(body: str, token: str | None = None) -> InboundEmail:
    return InboundEmail(
        event_id="e",
        secret_address_local="nora-abc123",
        from_address="ceo@example.com",
        dkim_verified=True,
        body=body,
        correlation_token=token,
    )


# A thread where the CEO (the verified forwarder) has a prior reply in-thread.
THREAD_WITH_CEO_REPLY = """WIYOW

On Mon, Jun 9, 2026 at 10:00 AM CEO <ceo@example.com> wrote:
> Thanks Sarah, happy to chat. Tuesday works for me.
>
> On Sun, Jun 8, 2026 at 2:00 PM Sarah Lee <sarah@acme.com> wrote:
> > Could you share a time to talk about the partnership?
"""


def test_voice_exemplar_populated_from_ceo_prior_reply() -> None:
    req = parse_request(_email(THREAD_WITH_CEO_REPLY))
    assert "Thanks Sarah, happy to chat." in req.voice_exemplar
    # counterpart text is not part of the exemplar
    assert "Could you share a time" not in req.voice_exemplar


def test_voice_exemplar_empty_on_cold_thread() -> None:
    # GMAIL fixture has only Sarah's message — no prior CEO reply → generic fallback.
    req = parse_request(_email(GMAIL))
    assert req.voice_exemplar == ""


def test_gmail_wiyow() -> None:
    req = parse_request(_email(GMAIL))
    assert req.command is Command.WIYOW
    assert req.thread.original_sender == "sarah@acme.com"
    assert req.recipient_uncertain is False
    assert req.thread.subject == "Proposal"


def test_apple_summarize() -> None:
    req = parse_request(_email(APPLE))
    assert req.command is Command.S
    assert req.thread.original_sender == "sarah@acme.com"


def test_outlook_natural_language() -> None:
    req = parse_request(_email(OUTLOOK))
    assert req.command is Command.NONE
    assert req.instruction_text == "reply declining politely"
    assert req.thread.original_sender == "sarah@acme.com"


def test_code_normalized_case_and_trailing() -> None:
    assert detect_command("wiyow ")[0] is Command.WIYOW
    assert detect_command("S.")[0] is Command.S


def test_code_inside_quoted_block_is_not_a_command() -> None:
    body = (
        "---------- Forwarded message ----------\n"
        "From: Sarah Lee <sarah@acme.com>\n"
        "To: CEO <ceo@example.com>\n"
        "Subject: Proposal\n\n"
        "WIYOW was a great suggestion in our last call.\n"
    )
    req = parse_request(_email(body))
    assert req.command is Command.NONE  # the code is in quoted content, not authored


def test_multiparty_thread_flags_recipient_uncertain() -> None:
    req = parse_request(_email(MULTIPARTY))
    assert req.command is Command.WIYOW
    assert req.recipient_uncertain is True  # two distinct senders in the chain


def test_no_parseable_header_flags_uncertain() -> None:
    req = parse_request(_email("WIYOW\n\n>>> garbled quoted text with no headers"))
    assert req.recipient_uncertain is True
    assert req.thread.original_sender is None


def test_incidental_code_on_later_line_not_a_command() -> None:
    # A bare 'S' below the first authored line must not misroute to summarize.
    body = (
        "Please reply to Sarah declining.\nS\n\n"
        "---------- Forwarded message ----------\n"
        "From: Sarah <sarah@acme.com>\nTo: CEO <ceo@example.com>\nSubject: Proposal\n\nHi.\n"
    )
    req = parse_request(_email(body))
    assert req.command is Command.NONE
    assert req.instruction_text is not None and "reply" in req.instruction_text.lower()


def test_correlation_token_carried_through() -> None:
    req = parse_request(_email(GMAIL, token="corr99"))
    assert req.correlation_token == "corr99"


def test_split_authored_region_handles_no_quote() -> None:
    authored, quoted = split_authored_region("just an instruction, no quote")
    assert authored == "just an instruction, no quote"
    assert quoted == ""


def test_prose_from_line_does_not_truncate_authored_region() -> None:
    # A CEO line starting "From:" is prose, not a forwarded header — a following KL
    # code must stay in the authored region (else keep-local would fail open).
    body = "RE\nFrom: my read of it, keep this private\nNB-RE-KL\n\n> quoted original"
    authored, quoted = split_authored_region(body)
    assert "NB-RE-KL" in authored
    assert "KL" not in quoted.replace("quoted", "")  # the KL code is not in the quote


def test_real_from_header_with_email_is_still_a_boundary() -> None:
    body = "RE\nFrom: Sarah Lee <sarah@acme.com>\nHi CEO"
    authored, quoted = split_authored_region(body)
    assert authored == "RE"
    assert quoted.startswith("From: Sarah Lee")


def test_from_header_run_without_email_is_still_a_boundary() -> None:
    body = "RE\nFrom: Sarah Lee\nSent: Monday\nSubject: Proposal\nHi"
    authored, quoted = split_authored_region(body)
    assert authored == "RE"
    assert quoted.startswith("From: Sarah Lee")
