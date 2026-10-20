"""Mailbox providers: apply canonical inbox actions + locate a message, per provider. Gmail is
implemented (gmail.modify — safe label ops only); Microsoft Graph (Outlook) is a documented seam
for when an Outlook account is connected. The canonical action strings come from
app/inbox_actions.py; each provider translates them to its own primitives."""

from __future__ import annotations

import re
from typing import Any, Protocol

# Canonical action -> (add|remove, Gmail system label). "label:<NAME>" is handled separately
# (get-or-create a custom label). Trash/delete are intentionally absent — safe/reversible only.
_GMAIL_SYS = {
    "star": ("add", "STARRED"),
    "archive": ("remove", "INBOX"),
    "read": ("remove", "UNREAD"),
    "unread": ("add", "UNREAD"),
    "important": ("add", "IMPORTANT"),
}
_FWD_PREFIX = re.compile(r"^(?:re|fwd|fw)\s*:\s*", re.IGNORECASE)


class MailboxProvider(Protocol):
    def find_message(self, sender: str, subject: str, rfc822msgid: str | None) -> str | None: ...
    def apply_actions(self, message_id: str, actions: list[str]) -> None: ...


class NullMailboxProvider:
    """Default: no mailbox wired — actions no-op, find finds nothing."""

    def find_message(self, sender: str, subject: str, rfc822msgid: str | None) -> str | None:
        return None

    def apply_actions(self, message_id: str, actions: list[str]) -> None:
        return None


class GmailMailboxProvider:
    """Applies safe inbox actions to a Gmail message via label add/remove (gmail.modify). No
    trash/delete. ``service`` is a Gmail API resource; ``user_id`` is "me" for OAuth."""

    def __init__(self, service: Any, user_id: str = "me") -> None:
        self._svc = service
        self._user = user_id
        self._label_ids: dict[str, str] | None = None  # name -> id cache (custom labels)

    def _custom_label_id(self, name: str) -> str:
        if self._label_ids is None:
            labels = self._svc.users().labels().list(userId=self._user).execute().get("labels", [])
            self._label_ids = {lbl["name"]: lbl["id"] for lbl in labels}
        if name in self._label_ids:
            return self._label_ids[name]
        created = (
            self._svc.users().labels()
            .create(userId=self._user, body={
                "name": name, "labelListVisibility": "labelShow", "messageListVisibility": "show",
            })
            .execute()
        )
        self._label_ids[name] = created["id"]
        return created["id"]

    def apply_actions(self, message_id: str, actions: list[str]) -> None:
        add: list[str] = []
        remove: list[str] = []
        for a in actions or []:
            if a.startswith("label:"):
                add.append(self._custom_label_id(a[len("label:"):]))
            elif a in _GMAIL_SYS:
                op, lid = _GMAIL_SYS[a]
                (add if op == "add" else remove).append(lid)
        if not add and not remove:
            return
        body: dict[str, list[str]] = {}
        if add:
            body["addLabelIds"] = add
        if remove:
            body["removeLabelIds"] = remove
        self._svc.users().messages().modify(userId=self._user, id=message_id, body=body).execute()

    def find_message(self, sender: str, subject: str, rfc822msgid: str | None) -> str | None:
        if rfc822msgid:
            q = f"rfc822msgid:{rfc822msgid}"
        else:
            subj = _FWD_PREFIX.sub("", (subject or "").strip())
            parts = []
            if sender:
                parts.append(f"from:{sender}")
            if subj:
                parts.append(f'subject:"{subj}"')
            if not parts:
                return None
            q = " ".join(parts)
        msgs = (
            self._svc.users().messages()
            .list(userId=self._user, q=q, maxResults=1).execute().get("messages") or []
        )
        return msgs[0]["id"] if msgs else None


class GraphMailboxProvider:
    """Applies safe inbox actions to an Outlook / Microsoft 365 message via Microsoft Graph
    (Mail.ReadWrite). No delete. ``session`` is an httpx-like client carrying an app-only bearer
    token (same shape as GraphDraftTenantClient's); ``tenant_user`` is the target mailbox UPN.
    Canonical actions map to Graph primitives: star→flag, archive→move to the Archive well-known
    folder, read/unread→isRead, important→importance:high, label:X→category (unioned with existing).
    Everything the PATCH can carry goes in one call; archive is a separate /move (Graph's model)."""

    def __init__(
        self, tenant_user: str, *, session: Any, base_url: str = "https://graph.microsoft.com/v1.0"
    ) -> None:
        self._user = tenant_user
        self._session = session
        self._base = base_url.rstrip("/")

    def _msg_url(self, message_id: str) -> str:
        return f"{self._base}/users/{self._user}/messages/{message_id}"

    def apply_actions(self, message_id: str, actions: list[str]) -> None:
        acts = set(actions or [])
        cats = [a[len("label:"):] for a in (actions or []) if a.startswith("label:")]
        patch: dict[str, Any] = {}
        if "read" in acts:
            patch["isRead"] = True
        if "unread" in acts:
            patch["isRead"] = False
        if "important" in acts:
            patch["importance"] = "high"
        if "star" in acts:
            patch["flag"] = {"flagStatus": "flagged"}
        if cats:
            resp = self._session.get(self._msg_url(message_id) + "?$select=categories")
            existing = resp.json().get("categories") or []
            patch["categories"] = list(dict.fromkeys([*existing, *cats]))
        if patch:
            self._session.patch(self._msg_url(message_id), json=patch).raise_for_status()
        if "archive" in acts:
            self._session.post(
                self._msg_url(message_id) + "/move", json={"destinationId": "archive"}
            ).raise_for_status()

    def find_message(self, sender: str, subject: str, rfc822msgid: str | None) -> str | None:
        base = f"{self._base}/users/{self._user}/messages"
        if rfc822msgid:
            url = f"{base}?$filter=internetMessageId eq '{rfc822msgid}'&$top=1&$select=id"
        else:
            subj = _FWD_PREFIX.sub("", (subject or "").strip())
            terms = []
            if sender:
                terms.append(f"from:{sender}")
            if subj:
                terms.append(f"subject:{subj}")
            if not terms:
                return None
            url = f'{base}?$search="{" ".join(terms)}"&$top=1&$select=id'
        items = self._session.get(url).json().get("value") or []
        return items[0]["id"] if items else None
