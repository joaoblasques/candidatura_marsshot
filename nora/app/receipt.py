"""Receipt + reply composition (U6).

Builds the single reply Nora sends to the CEO. For a draft, the clean
copy-ready block sits at the very top (R8) so copying it is the path of least
resistance and the four-part receipt below never leaks into what the CEO sends
(R6). Summaries and clarifying questions carry the same four-part receipt
adapted to the case (R5).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.models import NoraResult, OutboundAttachment, ResultKind

# Copy markers delimiting the sendable draft. Everything strictly between them is
# the draft and nothing else (R6/AE6).
DRAFT_START = "----- copy the reply below this line -----"
DRAFT_END = "----- copy the reply above this line -----"

_STATUS = "Nothing was sent — this is a draft for you to review."


@dataclass
class ComposedReply:
    subject: str
    body: str
    attachments: list[OutboundAttachment] = field(default_factory=list)


def extract_draft_block(body: str) -> str | None:
    """Return the text between the copy markers, or None if absent.

    Mirrors what a CEO copies; tests assert it contains no receipt text.
    """
    if DRAFT_START not in body or DRAFT_END not in body:
        return None
    after = body.split(DRAFT_START, 1)[1]
    return after.split(DRAFT_END, 1)[0].strip()


def _how_to(kind: ResultKind) -> str:
    if kind is ResultKind.DRAFT:
        return "To revise, reply to me with changes (e.g. \"make it warmer\" or \"shorter\")."
    if kind is ResultKind.SUMMARY:
        return "Nothing to send. Reply RE (or WIYOW) if you'd like me to draft a response."
    if kind in (ResultKind.ANALYSIS, ResultKind.REPORT):
        return "Nothing to send — this is for your review. Reply RE if you'd like a draft."
    if kind is ResultKind.DEFERRED:
        return (
            "I can draft (RE), summarize (SC/S), analyze (AC), or write a report (PR) — "
            "forward the thread with one of those."
        )
    if kind is ResultKind.CLARIFY:
        return "Reply RE to draft, SC to summarize, AC to analyze, or PR for a report."
    return "Forward the original thread to me again to start over."


def _readback(result: NoraResult) -> str:
    if result.interpretation_readback:
        return result.interpretation_readback
    if result.kind is ResultKind.CLARIFY:
        return "I couldn't tell what you wanted me to do with this."
    if result.kind is ResultKind.LOST_THREAD:
        return "I couldn't match your reply to an earlier draft."
    return ""


def _receipt(result: NoraResult) -> str:
    lines = ["— Nora —", f"Read as: {_readback(result)}", _STATUS, _how_to(result.kind)]
    if result.uncertainty:
        lines.append(f"Heads up: {result.uncertainty}")
    # Layer C v2 (auto-learn): transparently surface what Nora learned about the counterparty
    # this turn — only when there's something new — and how to undo it.
    if result.derived_facts:
        lines.append(f"Learned: {' · '.join(result.derived_facts)} (reply FORGET to drop)")
    return "\n".join(lines)


def _subject(result: NoraResult, thread_subject: str | None) -> str:
    subj = thread_subject or "your thread"
    if result.kind is ResultKind.DRAFT:
        who = "recipient unconfirmed" if result.recipient_uncertain or not result.recipient else result.recipient
        return f"Nora · draft reply to {who} — {subj}"
    if result.kind is ResultKind.SUMMARY:
        return f"Nora · summary — {subj}"
    if result.kind is ResultKind.ANALYSIS:
        return f"Nora · analysis — {subj}"
    if result.kind is ResultKind.REPORT:
        return f"Nora · report — {subj}"
    if result.kind is ResultKind.DEFERRED:
        return "Nora · not available in this version"
    if result.kind is ResultKind.PROPOSAL:
        who = result.recipient or "the other party"
        return f"Nora · proposed meeting email to {who} — {subj}"
    if result.kind is ResultKind.ACTION_SENT:
        who = result.recipient or "the other party"
        return f"Nora · sent to {who} — {subj}"
    if result.kind is ResultKind.ACTION_DECLINED:
        return f"Nora · cancelled — {subj}"
    if result.kind is ResultKind.CLARIFY:
        return "Nora · quick question"
    return "Nora · couldn't find that thread"


def compose_reply(result: NoraResult, thread_subject: str | None = None) -> ComposedReply:
    """Compose the CEO-facing reply for any result kind."""
    if result.kind is ResultKind.PROPOSAL:
        who = result.recipient or "the other party"
        confirm = (
            f"Nothing has been sent. Reply AG to send this to {who} from my own "
            "assistant address (on the CEO's behalf — never as the CEO), or ANG to cancel."
        )
        parts = ["— Nora —", f"Read as: {_readback(result)}", confirm]
        if result.uncertainty:
            parts.append(f"Heads up: {result.uncertainty}")
        body = (
            f"Proposed email — to: {who}\n"
            f"{DRAFT_START}\n{result.text}\n{DRAFT_END}\n\n" + "\n".join(parts)
        )
        return ComposedReply(subject=_subject(result, thread_subject), body=body)
    if result.kind in (ResultKind.ACTION_SENT, ResultKind.ACTION_DECLINED):
        # The text is the outcome message composed by the confirm/send path.
        return ComposedReply(subject=_subject(result, thread_subject), body=f"{result.text}\n\n— Nora —")

    receipt = _receipt(result)
    if result.kind is ResultKind.DRAFT:
        body = f"{DRAFT_START}\n{result.text}\n{DRAFT_END}\n\n{receipt}"
    elif result.kind is ResultKind.SUMMARY:
        body = f"{result.text}\n\n{receipt}"
    else:  # CLARIFY or LOST_THREAD — the question/notice is the text
        body = f"{result.text}\n\n{receipt}" if result.text else receipt
    return ComposedReply(subject=_subject(result, thread_subject), body=body)
