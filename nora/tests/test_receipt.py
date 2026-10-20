"""U6: receipt + reply composition."""

from __future__ import annotations

from app.receipt import DRAFT_START, compose_reply, extract_draft_block
from app.models import NoraResult, ResultKind

DRAFT = NoraResult(
    kind=ResultKind.DRAFT,
    text="Hi Sarah, thanks for the proposal — we'll pass for now. Best.",
    interpretation_readback="Decline Sarah's proposal politely.",
    recipient="sarah@acme.com",
)


def test_receipt_surfaces_learned_facts() -> None:
    r = DRAFT.model_copy(update={"derived_facts": ["prefers mornings", "is the Brightwave lead"]})
    reply = compose_reply(r, thread_subject="Proposal")
    assert "Learned: prefers mornings · is the Brightwave lead (reply FORGET to drop)" in reply.body
    # The Learned line lives in the receipt region, never inside the copyable draft block.
    assert "Learned:" not in extract_draft_block(reply.body)


def test_receipt_omits_learned_line_when_no_new_facts() -> None:
    reply = compose_reply(DRAFT, thread_subject="Proposal")
    assert "Learned:" not in reply.body


def test_draft_block_is_at_top_and_copy_clean() -> None:
    reply = compose_reply(DRAFT, thread_subject="Proposal")
    # Draft block precedes the receipt.
    assert reply.body.index(DRAFT_START) < reply.body.index("— Nora —")
    block = extract_draft_block(reply.body)
    assert block == DRAFT.text
    # Copying the block yields no receipt text. (Covers AE6.)
    for receipt_word in ("Read as:", "Nothing was sent", "To revise", "Nora"):
        assert receipt_word not in block


def test_draft_receipt_has_four_parts() -> None:
    body = compose_reply(DRAFT, "Proposal").body
    assert "Read as:" in body  # (a) interpretation readback
    assert "Nothing was sent" in body  # (b) status
    assert "To revise" in body  # (c) how to revise
    # (d) uncertainty only when present — none here.


def test_draft_subject_names_recipient_and_gist() -> None:
    reply = compose_reply(DRAFT, "Proposal")
    assert "sarah@acme.com" in reply.subject and "Proposal" in reply.subject


def test_uncertain_recipient_in_subject_and_receipt() -> None:
    uncertain = DRAFT.model_copy(
        update={"recipient_uncertain": True, "uncertainty": "Verify the recipient."}
    )
    reply = compose_reply(uncertain, "Proposal")
    assert "recipient unconfirmed" in reply.subject
    assert "Heads up: Verify the recipient." in reply.body


def test_summary_has_no_draft_block_or_send_guidance() -> None:
    summary = NoraResult(
        kind=ResultKind.SUMMARY,
        text="Sarah sent a proposal; awaiting your decision.",
        interpretation_readback="Summarize the thread.",
    )
    reply = compose_reply(summary, "Proposal")
    assert extract_draft_block(reply.body) is None
    assert "Nothing to send" in reply.body  # (covers AE7's no-send guidance)
    assert reply.subject.startswith("Nora · summary")


def test_clarify_has_question_and_no_draft_block() -> None:
    clarify = NoraResult(kind=ResultKind.CLARIFY, text="Draft a reply or summarize?")
    reply = compose_reply(clarify)
    assert "Draft a reply or summarize?" in reply.body
    assert extract_draft_block(reply.body) is None
    assert reply.subject == "Nora · quick question"
