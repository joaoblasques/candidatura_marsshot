"""Files domain — ACCESS (read), Layer A. A referenced company file is read into the
draft context as fenced DATA. Read-only and **no consent gate**: the principal's own file
feeding the principal's own draft, and (per rung) nothing leaves that it shouldn't — text
is extracted with the same hardened extractor as inbound attachments.

See docs/brainstorms/2026-06-18-files-domain-brainstorm.md. Default backend is Null (off).
"""

from __future__ import annotations

import mimetypes
import os
import re
from typing import Any, Protocol

from app.attachments.extract import extract_attachments
from app.models import Attachment, OutboundAttachment

# Google-native types we export to a text format the extractor can read (for reading).
_GOOGLE_EXPORT = {
    "application/vnd.google-apps.document": ("text/plain", ".txt"),
    "application/vnd.google-apps.spreadsheet": ("text/csv", ".csv"),
    "application/vnd.google-apps.presentation": ("text/plain", ".txt"),
}
# For SHARING (the actual file), export Google-native docs to a portable format.
_SHARE_MAX_BYTES = 25_000_000  # don't attach a file larger than a typical email limit

_GOOGLE_SHARE_EXPORT = {
    "application/vnd.google-apps.document": ("application/pdf", ".pdf"),
    "application/vnd.google-apps.spreadsheet": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".xlsx"),
    "application/vnd.google-apps.presentation": ("application/pdf", ".pdf"),
}


class FileSource(Protocol):
    def read(self, ref: str) -> str | None:
        """Resolve ``ref`` (a file name) and return bounded extracted TEXT (for drafting
        context) — or None if not found / unreadable. Never raises."""
        ...

    def fetch(self, ref: str) -> OutboundAttachment | None:
        """Resolve ``ref`` and return the actual FILE (for sharing) as an attachment —
        or None if not found. Native docs are exported to a portable format. Never raises."""
        ...

    def fetch_by_id(self, file_id: str) -> OutboundAttachment | None:
        """Like ``fetch`` but resolve by the store's exact file ID rather than a name (used where
        the caller already holds an unambiguous id, e.g. an approved-file catalog). Never raises."""
        ...

    def grant(self, ref: str, recipient: str) -> str | None:
        """Grant ``recipient`` read access to ``ref`` in the store, WITHOUT a store-sent
        notification (Nora sends the link from her own address). Returns a shareable link,
        or None if not found / unsupported. Never raises."""
        ...

    def search(self, ref: str) -> list[str]:
        """Return candidate file names matching ``ref`` (metadata only, no download), so an
        ambiguous reference can be disambiguated before a share. [] if none. Never raises."""
        ...

    def create(self, filename: str, content: bytes, content_type: str) -> str | None:
        """Create a NEW file in the store (Nora filing her own artifact) and return a link,
        or None if unsupported / failed. Not a third-party action. Never raises."""
        ...


_SEARCH_LIMIT = 5  # candidate names to surface when a reference is ambiguous

# Noise words a fuzzy descriptor may carry in (the NL extractor already strips most fetch
# verbs, but be defensive). Kept small so it never swallows a real filename term.
_SEARCH_STOPWORDS = frozenset({
    "the", "a", "an", "of", "for", "to", "my", "me", "please", "file", "files",
    "and", "this", "that", "it", "read", "summarize", "get", "find", "pull", "up",
    "fetch", "send", "grab",
})


def _search_tokens(ref: str) -> list[str]:
    """Tokenize a (possibly fuzzy) file reference into Drive ``name contains`` terms.

    Splits on whitespace + common filename separators, strips possessives/punctuation,
    drops stopwords and 1-char noise, neutralizes the query literal, and caps the term
    count. Every term is ANDed by the caller, so ``"Q4's earnings"`` resolves
    ``Q4-2026-earnings.docx`` while ``"earnings"`` alone surfaces all four quarters."""
    if not ref:
        return []
    seen: set[str] = set()
    tokens: list[str] = []
    for piece in re.split(r"[\s\-_.,/]+", ref.strip()):
        t = piece.strip().strip("'\"")
        for suffix in ("'s", "’s"):
            if t.endswith(suffix):
                t = t[: -len(suffix)]
        t = t.replace("\\", "").replace("'", "")  # neutralize the query literal
        low = t.lower()
        if len(t) < 2 or low in _SEARCH_STOPWORDS or low in seen:
            continue
        seen.add(low)
        tokens.append(t)
        if len(tokens) >= 6:
            break
    return tokens


class NullFileSource:
    """Default: no file source — file references resolve to nothing (feature off)."""

    def read(self, ref: str) -> str | None:
        return None

    def fetch(self, ref: str) -> OutboundAttachment | None:
        return None

    def fetch_by_id(self, file_id: str) -> OutboundAttachment | None:
        return None

    def grant(self, ref: str, recipient: str) -> str | None:
        return None

    def search(self, ref: str) -> list[str]:
        return []

    def create(self, filename: str, content: bytes, content_type: str) -> str | None:
        return None


class GoogleDriveFileSource:
    """Reads a file by name from the tenant's Google Drive (read-only). ``service`` is a
    Drive API resource (injected in tests). Google-native docs are exported to text;
    everything else is downloaded and run through the attachment extractor."""

    def __init__(self, *, service: Any, write_service: Any | None = None) -> None:
        self._service = service
        # Reads use the (read-only) ``service``; writes — create()/grant() — use
        # ``write_service`` when provided (a full-`drive` service), else fall back to
        # ``service`` (so a single read-write service still works in tests). This keeps the
        # read path least-privilege even when the write seam is enabled.
        self._write_service = write_service or service

    def read(self, ref: str) -> str | None:
        ref = (ref or "").strip()
        if not ref:
            return None
        try:
            safe = ref.replace("\\", "").replace("'", "")  # neutralize the query literal
            listing = (
                self._service.files()
                .list(
                    q=f"name = '{safe}' and trashed = false",
                    fields="files(id, name, mimeType)",
                    pageSize=1,
                )
                .execute()
            )
            files = listing.get("files") or []
            if not files:
                return None
            f = files[0]
            fid, mime, name = f.get("id"), f.get("mimeType", ""), f.get("name", ref)
            if mime in _GOOGLE_EXPORT:
                export_mime, ext = _GOOGLE_EXPORT[mime]
                content = self._service.files().export_media(fileId=fid, mimeType=export_mime).execute()
                name = name if name.endswith(ext) else name + ext
            else:
                content = self._service.files().get_media(fileId=fid).execute()
            data = content if isinstance(content, bytes) else bytes(content)
        except Exception:  # noqa: BLE001 — a store/API error degrades to "no file", never raises
            return None
        [extracted] = extract_attachments([Attachment(filename=name, size=len(data), content=data)])
        return extracted.text if extracted.status == "read" else None

    def fetch(self, ref: str) -> OutboundAttachment | None:
        ref = (ref or "").strip()
        if not ref:
            return None
        try:
            safe = ref.replace("\\", "").replace("'", "")
            listing = (
                self._service.files()
                .list(
                    q=f"name = '{safe}' and trashed = false",
                    fields="files(id, name, mimeType)",
                    pageSize=1,
                )
                .execute()
            )
            files = listing.get("files") or []
            if not files:
                return None
            f = files[0]
            fid, mime, name = f.get("id"), f.get("mimeType", ""), f.get("name", ref)
            if mime in _GOOGLE_SHARE_EXPORT:
                export_mime, ext = _GOOGLE_SHARE_EXPORT[mime]
                content = self._service.files().export_media(fileId=fid, mimeType=export_mime).execute()
                name = name if name.endswith(ext) else name + ext
                ctype = export_mime
            else:
                content = self._service.files().get_media(fileId=fid).execute()
                ctype = mime or "application/octet-stream"
            data = content if isinstance(content, bytes) else bytes(content)
        except Exception:  # noqa: BLE001 — a store/API error degrades to "no file", never raises
            return None
        if len(data) > _SHARE_MAX_BYTES:  # too large to attach — fail closed (no share)
            return None
        return OutboundAttachment(filename=name, content=data, content_type=ctype)

    def fetch_by_id(self, file_id: str) -> OutboundAttachment | None:
        file_id = (file_id or "").strip()
        if not file_id:
            return None
        try:
            meta = self._service.files().get(fileId=file_id, fields="id, name, mimeType").execute()
            mime, name = meta.get("mimeType", ""), meta.get("name", file_id)
            if mime in _GOOGLE_SHARE_EXPORT:
                export_mime, ext = _GOOGLE_SHARE_EXPORT[mime]
                content = self._service.files().export_media(fileId=file_id, mimeType=export_mime).execute()
                name = name if name.endswith(ext) else name + ext
                ctype = export_mime
            else:
                content = self._service.files().get_media(fileId=file_id).execute()
                ctype = mime or "application/octet-stream"
            data = content if isinstance(content, bytes) else bytes(content)
        except Exception:  # noqa: BLE001 — a store/API error degrades to "no file", never raises
            return None
        if len(data) > _SHARE_MAX_BYTES:  # too large to attach — fail closed
            return None
        return OutboundAttachment(filename=name, content=data, content_type=ctype)

    def grant(self, ref: str, recipient: str) -> str | None:
        ref, recipient = (ref or "").strip(), (recipient or "").strip()
        if not ref or not recipient:
            return None
        try:
            safe = ref.replace("\\", "").replace("'", "")
            listing = (
                self._service.files()
                .list(q=f"name = '{safe}' and trashed = false",
                      fields="files(id, name)", pageSize=1)
                .execute()
            )
            files = listing.get("files") or []
            if not files:
                return None
            fid = files[0].get("id")
            # Silent grant (no Drive-sent email — Nora notifies from her own address).
            self._write_service.permissions().create(
                fileId=fid,
                body={"type": "user", "role": "reader", "emailAddress": recipient},
                sendNotificationEmail=False,
                fields="id",
            ).execute()
            meta = self._service.files().get(fileId=fid, fields="webViewLink").execute()
        except Exception:  # noqa: BLE001 — a store/API error degrades to "no link", never raises
            return None
        return meta.get("webViewLink")

    def search(self, ref: str) -> list[str]:
        # Fuzzy: AND a `name contains` clause per token so a descriptor ("Q4's earnings")
        # resolves a hyphenated filename ("Q4-2026-earnings.docx") and a single term
        # ("earnings") surfaces every match for the caller to disambiguate.
        tokens = _search_tokens(ref)
        if not tokens:
            return []
        try:
            clauses = " and ".join(f"name contains '{t}'" for t in tokens)
            listing = (
                self._service.files()
                .list(q=f"({clauses}) and trashed = false",
                      fields="files(name)", pageSize=_SEARCH_LIMIT)
                .execute()
            )
        except Exception:  # noqa: BLE001
            return []
        return [f.get("name", "") for f in (listing.get("files") or []) if f.get("name")]

    def create(self, filename: str, content: bytes, content_type: str) -> str | None:
        from googleapiclient.http import MediaInMemoryUpload

        try:
            created = self._write_service.files().create(
                body={"name": filename},
                media_body=MediaInMemoryUpload(content, mimetype=content_type),
                fields="id, webViewLink",
            ).execute()
        except Exception:  # noqa: BLE001 — degrade to "no link", never raises
            return None
        return created.get("webViewLink")


class GraphFileSource:
    """Reads a file by name from the tenant's Microsoft 365 OneDrive (via Microsoft
    Graph, read-only). ``session`` is an httpx-like client carrying an app-only bearer
    token (injected in tests). OneDrive stores Office files as real binaries, so no
    export step is needed — download and run through the attachment extractor."""

    def __init__(
        self, tenant_user: str, *, session: Any, base_url: str = "https://graph.microsoft.com/v1.0"
    ) -> None:
        self._user = tenant_user
        self._session = session
        self._base = base_url.rstrip("/")

    def _resolve(self, ref: str) -> tuple[bytes, str] | None:
        safe = ref.replace("'", "")  # neutralize the search literal
        resp = self._session.get(
            f"{self._base}/users/{self._user}/drive/root/search(q='{safe}')",
            params={"$top": "1", "$select": "id,name"},
        )
        resp.raise_for_status()
        items = (resp.json() or {}).get("value") or []
        if not items:
            return None
        item_id, name = items[0].get("id"), items[0].get("name", ref)
        if not item_id:
            return None
        content = self._session.get(f"{self._base}/users/{self._user}/drive/items/{item_id}/content")
        content.raise_for_status()
        return content.content, name

    def read(self, ref: str) -> str | None:
        ref = (ref or "").strip()
        if not ref:
            return None
        try:
            resolved = self._resolve(ref)
        except Exception:  # noqa: BLE001 — a store/API error degrades to "no file", never raises
            return None
        if resolved is None:
            return None
        data, name = resolved
        [extracted] = extract_attachments([Attachment(filename=name, size=len(data), content=data)])
        return extracted.text if extracted.status == "read" else None

    def fetch(self, ref: str) -> OutboundAttachment | None:
        ref = (ref or "").strip()
        if not ref:
            return None
        try:
            resolved = self._resolve(ref)
        except Exception:  # noqa: BLE001
            return None
        if resolved is None:
            return None
        data, name = resolved
        if len(data) > _SHARE_MAX_BYTES:
            return None
        ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
        return OutboundAttachment(filename=name, content=data, content_type=ctype)

    def grant(self, ref: str, recipient: str) -> str | None:
        ref, recipient = (ref or "").strip(), (recipient or "").strip()
        if not ref or not recipient:
            return None
        try:
            safe = ref.replace("'", "")
            resp = self._session.get(
                f"{self._base}/users/{self._user}/drive/root/search(q='{safe}')",
                params={"$top": "1", "$select": "id,name,webUrl"},
            )
            resp.raise_for_status()
            items = (resp.json() or {}).get("value") or []
            if not items:
                return None
            item = items[0]
            invite = self._session.post(
                f"{self._base}/users/{self._user}/drive/items/{item['id']}/invite",
                json={
                    "recipients": [{"email": recipient}],
                    "roles": ["read"],
                    "sendInvitation": False,  # silent — Nora notifies from her own address
                    "requireSignIn": True,
                },
            )
            invite.raise_for_status()
            link = (((invite.json() or {}).get("value") or [{}])[0].get("link") or {}).get("webUrl")
        except Exception:  # noqa: BLE001 — degrade to "no link", never raises
            return None
        return link or item.get("webUrl")

    def search(self, ref: str) -> list[str]:
        ref = (ref or "").strip()
        if not ref:
            return []
        try:
            safe = ref.replace("'", "")
            resp = self._session.get(
                f"{self._base}/users/{self._user}/drive/root/search(q='{safe}')",
                params={"$top": str(_SEARCH_LIMIT), "$select": "name"},
            )
            resp.raise_for_status()
            items = (resp.json() or {}).get("value") or []
        except Exception:  # noqa: BLE001
            return []
        return [it.get("name", "") for it in items if it.get("name")]

    def create(self, filename: str, content: bytes, content_type: str) -> str | None:
        try:
            resp = self._session.put(
                f"{self._base}/users/{self._user}/drive/root:/{filename}:/content",
                content=content,
                headers={"Content-Type": content_type},
            )
            resp.raise_for_status()
            return (resp.json() or {}).get("webUrl")
        except Exception:  # noqa: BLE001 — degrade to "no link", never raises
            return None


class LocalFileVault:
    """Reads a file by name from an in-boundary directory (R3 — strictest rung). No
    network: for the rung where nothing may leave the boundary, so it reads locally and
    **never shares out** (``fetch`` returns None). Path-contained to a single root: any
    ref that resolves outside the root (``..``, an absolute path, a boundary-crossing
    symlink) is rejected."""

    def __init__(self, root: str) -> None:
        self._root = os.path.realpath(root) if root else ""

    def _safe_path(self, ref: str) -> str | None:
        if not self._root or not ref:
            return None
        target = os.path.realpath(os.path.join(self._root, ref))
        # Containment: target must be the root itself or strictly under it.
        if target != self._root and not target.startswith(self._root + os.sep):
            return None
        return target

    def read(self, ref: str) -> str | None:
        path = self._safe_path((ref or "").strip())
        if not path or not os.path.isfile(path):
            return None
        try:
            with open(path, "rb") as fh:
                data = fh.read(_SHARE_MAX_BYTES + 1)
        except OSError:
            return None
        [extracted] = extract_attachments(
            [Attachment(filename=os.path.basename(path), size=len(data), content=data)]
        )
        return extracted.text if extracted.status == "read" else None

    def fetch(self, ref: str) -> OutboundAttachment | None:
        # R3 never shares a file out of the boundary — read-only by design.
        return None

    def grant(self, ref: str, recipient: str) -> str | None:
        # R3 never grants access out of the boundary — read-only by design.
        return None

    def search(self, ref: str) -> list[str]:
        # Single in-boundary root, exact-name model: present iff the contained path exists.
        path = self._safe_path((ref or "").strip())
        return [os.path.basename(path)] if path and os.path.isfile(path) else []

    def create(self, filename: str, content: bytes, content_type: str) -> str | None:
        # R3 in-boundary vault: writing files is out of scope for this slice.
        return None


_DRIVE_READONLY_SCOPE = "https://www.googleapis.com/auth/drive.readonly"
_DRIVE_WRITE_SCOPE = "https://www.googleapis.com/auth/drive"


def build_drive_service(credentials_json: str, tenant_user: str, *, scope: str = _DRIVE_READONLY_SCOPE) -> Any:
    """Build a domain-wide-delegated Google Drive service impersonating the tenant user.
    Defaults to read-only (``drive.readonly``) — the runtime files-access boundary. Lazy
    import so the google libs aren't required unless this backend is selected."""
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    creds = service_account.Credentials.from_service_account_file(
        credentials_json, scopes=[scope], subject=tenant_user
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def build_drive_write_service(credentials_json: str, tenant_user: str) -> Any:
    """Full-``drive`` delegated service for one-off seeding/authoring (e.g.
    scripts/seed_showcase_drive). NOT the runtime path, which stays read-only — the
    SA's domain-wide delegation must include the full ``drive`` scope for this."""
    return build_drive_service(credentials_json, tenant_user, scope=_DRIVE_WRITE_SCOPE)
