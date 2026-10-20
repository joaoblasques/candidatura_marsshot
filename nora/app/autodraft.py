"""Ambient auto-draft (connected mailbox). When a genuine person-to-person email lands, Nora
drafts a reply in the exec's voice and saves it to their Drafts (threaded) — she NEVER sends.
Default-off; posture-gated to R1; idempotent via a label (never marks the exec's mail read).

This module is pure orchestration + a deterministic inbound filter; drafting/recall live in the
Pipeline and the Drafts write in the tenant client (both injected)."""

from __future__ import annotations

import re
from typing import Any, Callable

from app.email.gmail_inbound import _from_address, _plain_text_body
from app.email.parser import parse_forwarded_thread, split_authored_region
from app.inbox_actions import parse_command

_ADDR_RE = re.compile(r"[\w.+-]+@[\w.-]+")

# From local-parts that are never a human you'd reply to.
_NON_HUMAN_LOCALPARTS = frozenset({
    "no-reply", "noreply", "do-not-reply", "donotreply", "mailer-daemon", "postmaster",
})
_BULK_PRECEDENCE = frozenset({"bulk", "list", "junk"})


def _lower_headers(headers: dict) -> dict[str, str]:
    return {str(k).strip().lower(): str(v) for k, v in (headers or {}).items()}


def _from_localpart(value: str) -> str:
    """Local-part of a From header value ('Name <a@b.com>' or 'a@b.com'), lowercased."""
    addr = value.split("<")[-1].split(">")[0] if "<" in value else value
    addr = addr.strip().strip("<>").lower()
    return addr.split("@", 1)[0] if "@" in addr else addr


def is_personal_inbound(headers: dict) -> bool:
    """True when an inbound looks like genuine person-to-person mail worth auto-drafting a reply.

    Deterministic and header-only — it makes NO judgment about importance/seriousness (that line
    stays parked). It only filters out bulk/list/automated/no-reply mail you'd never reply to."""
    h = _lower_headers(headers)
    if "list-unsubscribe" in h or "list-id" in h:
        return False
    if h.get("precedence", "").strip().lower() in _BULK_PRECEDENCE:
        return False
    auto = h.get("auto-submitted", "").strip().lower()
    if auto and auto != "no":
        return False
    if _from_localpart(h.get("from", "")) in _NON_HUMAN_LOCALPARTS:
        return False
    return True


def _headers_dict(payload: dict) -> dict[str, str]:
    return {
        str(h.get("name", "")).strip().lower(): str(h.get("value", ""))
        for h in (payload.get("headers") or [])
    }


def is_command(headers: dict, alias: str) -> bool:
    """True when a message was addressed to the command plus-alias (``<local>+<alias>@…``) — i.e.
    the user forwarded it to Nora with an inbox-action instruction."""
    marker = "+" + (alias or "").strip().lower()
    if not marker.strip("+"):
        return False
    for key in ("to", "cc", "delivered-to", "x-original-to"):
        for addr in _ADDR_RE.findall(headers.get(key, "").lower()):
            if addr.split("@", 1)[0].endswith(marker):
                return True
    return False


def apply_command(
    provider: Any, headers: dict, payload: dict, *,
    rules: Any = None, owner: str = "", save_draft: Callable[..., str] | None = None,
) -> tuple[str | None, list[str]]:
    """Handle an inbox-action command: apply safe actions to the ORIGINAL (from the forwarded
    quote) and, unless ``once``, store a per-sender rule so future mail is auto-handled. Controls:
    ``stop`` removes the sender's rule; ``rules?`` drafts a read-back of all rules. Returns
    (target_message_id, actions) for tests."""
    authored, quoted = split_authored_region(_plain_text_body(payload))
    cmd = parse_command(authored)
    thread = parse_forwarded_thread(quoted)
    sender = (thread.original_sender or "").strip()
    if cmd.control == "list":
        if save_draft is not None and rules is not None:
            active = rules.all(owner)
            listed = "\n".join(f"  {s}: {', '.join(a)}" for s, a in active.items()) or "  (none yet)"
            save_draft(owner, "Nora — your inbox rules", "Your inbox rules:\n" + listed)
        return None, []
    if cmd.control == "stop":
        if rules is not None and sender:
            rules.clear(owner, sender)
        return None, []
    if not cmd.actions:
        return None, []
    refs = (headers.get("references") or headers.get("in-reply-to") or "").strip()
    msgid = refs.split()[-1] if refs else None
    target = provider.find_message(sender, thread.subject or headers.get("subject", ""), msgid)
    if target:
        provider.apply_actions(target, cmd.actions)
    # Every command auto-becomes a per-sender rule (unless "once").
    if cmd.scope == "rule" and rules is not None and sender:
        rules.set(owner, sender, cmd.actions)
    return target, cmd.actions


def _thread_has_sent(service: Any, user_id: str, thread_id: str) -> bool:
    """True if the thread has any SENT message — i.e. the CEO replied (sent Nora's draft or their
    own). Conservative: if the thread can't be read, return True so a draft is never wrongly counted
    as a deletion."""
    if not thread_id:
        return False
    try:
        t = service.users().threads().get(userId=user_id, id=thread_id, format="minimal").execute()
    except Exception:  # noqa: BLE001 — ambiguity must never cause an over-suppression
        return True
    return any("SENT" in (m.get("labelIds") or []) for m in t.get("messages", []))


def reconcile_drafts(service: Any, feedback: Any, owner: str, user_id: str) -> None:
    """Resolve pending auto-drafts: still in Drafts → undecided; gone + a SENT reply on the thread →
    sent (reset the sender); gone + nothing sent → deleted (a negative signal, suppresses at 2)."""
    pend = feedback.pending(owner)
    if not pend:
        return
    existing = {
        d.get("id") for d in service.users().drafts().list(userId=user_id).execute().get("drafts", [])
    }
    for draft_id, (sender, thread_id) in pend.items():
        if draft_id in existing:
            continue  # still a draft — the CEO hasn't decided yet
        if _thread_has_sent(service, user_id, thread_id):
            feedback.record_sent(owner, sender)
        else:
            feedback.record_deletion(owner, sender)
        feedback.resolve(owner, draft_id)


def autodraft_once(
    service: Any, *, user_id: str, label_id: str,
    draft_reply: Callable[[str, str, str], Any],
    save_draft: Callable[..., str],
    query: str = "is:unread",
    provider: Any = None,
    alias: str | None = None,
    inbox_actions: bool = False,
    rules: Any = None,
    owner: str = "",
    filed_label_id: str | None = None,
    feedback: Any = None,
    draft_learning: bool = False,
) -> int:
    """One ambient sweep of a connected mailbox: for each matching message, draft a reply (when
    it's genuine person-to-person mail and the sender is R1) into the owner's Drafts, then LABEL
    the message so it's never reconsidered. NEVER removes UNREAD — the exec's read state is theirs.

    ``draft_reply(sender, subject, body) -> NoraResult | None`` and ``save_draft(to, subject, body,
    *, thread_id, in_reply_to)`` are injected so this stays testable and provider-agnostic. Returns
    the number of drafts created."""
    if draft_learning and feedback is not None:
        # Learn from the last sweep's drafts before making new ones (sent → keep; deleted → suppress).
        reconcile_drafts(service, feedback, owner, user_id)
    listing = (
        service.users().messages().list(userId=user_id, q=query, maxResults=25).execute()
    )
    drafted = 0
    for ref in listing.get("messages") or []:
        mid = ref.get("id")
        if not mid:
            continue
        msg = service.users().messages().get(userId=user_id, id=mid, format="full").execute()
        payload = msg.get("payload") or {}
        headers = _headers_dict(payload)
        is_cmd = inbox_actions and alias and provider is not None and is_command(headers, alias)
        if is_cmd:
            # An inbox-action command (forwarded to +alias): apply the actions to the original +
            # set/clear the per-sender rule; never draft a reply to the command itself.
            apply_command(provider, headers, payload, rules=rules, owner=owner, save_draft=save_draft)
        elif is_personal_inbound(headers):
            sender = headers.get("from", "")
            subject = headers.get("subject", "")
            # Per-sender rule: apply its safe actions to this message; if it archives, that mail is
            # filed away so skip the draft. Everything a rule touches gets Nora/filed (visibility).
            rule = rules.get(owner, _from_address(sender)) if (rules is not None and inbox_actions) else None
            if rule and provider is not None:
                provider.apply_actions(mid, rule)
                if filed_label_id:
                    service.users().messages().modify(
                        userId=user_id, id=mid, body={"addLabelIds": [filed_label_id]}
                    ).execute()
            sender_addr = _from_address(sender)
            # Draft-feedback learning: if the CEO keeps deleting Nora's drafts for this sender,
            # stop drafting for them (a rule that archives already suppresses too).
            suppressed = (
                draft_learning and feedback is not None and feedback.suppressed(owner, sender_addr)
            )
            if not (rule and "archive" in rule) and not suppressed:
                result = draft_reply(sender, subject, _plain_text_body(payload))
                if result is not None:
                    subj = subject if subject.lower().startswith("re:") else f"Re: {subject}".strip()
                    draft_id = save_draft(
                        sender_addr, subj, result.text,
                        thread_id=msg.get("threadId"), in_reply_to=headers.get("message-id"),
                    )
                    if draft_learning and feedback is not None:
                        feedback.track(owner, draft_id, sender_addr, msg.get("threadId") or "")
                    drafted += 1
        # Idempotency: label the message either way; never touch UNREAD (don't mark the exec's
        # mail read on their behalf). A command forward is ALSO archived (−INBOX) so it doesn't
        # linger and keep its conversation visible in the inbox after Nora has acted on it.
        body: dict[str, list[str]] = {"addLabelIds": [label_id]}
        if is_cmd:
            body["removeLabelIds"] = ["INBOX"]
        service.users().messages().modify(userId=user_id, id=mid, body=body).execute()
    return drafted
