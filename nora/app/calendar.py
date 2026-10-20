"""Calendar action seam (consent-gated). The provider is pluggable; the default
NullCalendarClient declines (no calendar configured), so a confirmed calendar hold fails
closed until a real backend is wired (operator/demand step). A real hold reaches
attendees, so it only ever runs after explicit CEO confirmation (AG)."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any, Protocol

from pydantic import BaseModel, Field

_DEFAULT_SLOT_MINUTES = 30

# dateparser's free-text search is over-eager (it will "find" a date in vague words
# like "sometime"). Only accept a match whose source phrase is genuinely date-like.
_DATEY = re.compile(
    r"\d"
    r"|\b(mon|tue|tues|wed|wednesday|thu|thur|thurs|fri|sat|sun"
    r"|monday|tuesday|thursday|friday|saturday|sunday)\b"
    r"|\b(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec"
    r"|january|february|march|april|june|july|august|september|october|november|december)\b"
    r"|\b(today|tomorrow|tonight|noon|midnight|am|pm)\b",
    re.IGNORECASE,
)


def parse_slot(
    text: str, *, now: datetime, default_minutes: int = _DEFAULT_SLOT_MINUTES, tz: str = "UTC"
) -> tuple[str, str] | None:
    """Best-effort: extract a concrete start/end (ISO 8601) from free text like
    "meet on 2026-07-01 15:00" or "Tuesday at 3pm". Times resolve in ``tz`` so the
    result is deterministic regardless of the host timezone. Returns None when no date
    is found (the caller keeps the placeholder). Never raises."""
    if not text or not text.strip():
        return None
    try:
        from dateparser.search import search_dates

        found = search_dates(
            text[:1000],
            settings={
                "RELATIVE_BASE": now,
                "PREFER_DATES_FROM": "future",
                "RETURN_AS_TIMEZONE_AWARE": True,
                "TIMEZONE": tz,
                "TO_TIMEZONE": tz,
            },
        )
    except Exception:  # noqa: BLE001 — NL date parsing must never break the propose path
        return None
    if not found:
        return None
    matched_text, start = found[0]
    if not _DATEY.search(matched_text):
        return None
    end = start + timedelta(minutes=default_minutes)
    return start.isoformat(), end.isoformat()


def _is_concrete(value: str) -> bool:
    """True when ``value`` parses as a real datetime (not a placeholder)."""
    try:
        datetime.fromisoformat(value)
        return True
    except (ValueError, TypeError):
        return False


class CalendarUnavailable(RuntimeError):
    """Raised when no calendar backend is configured to perform a confirmed hold."""


class CalendarEvent(BaseModel):
    summary: str
    # start/end are free-form today: the propose path stores a placeholder
    # ("(time to be confirmed)") since time parsing is deferred. A real CalendarClient must
    # parse/validate these into ISO 8601 (and reject/re-prompt on a placeholder) when wired.
    start: str
    end: str
    attendees: list[str] = Field(default_factory=list)


class CalendarClient(Protocol):
    def create_event(self, event: CalendarEvent, *, invite: bool = False) -> str:
        """Create the event; return a provider event id. ``invite=True`` sends a real
        invitation to the attendees (consent-gated); ``False`` is a private hold (nobody
        notified). Raise CalendarUnavailable if no real backend is configured."""
        ...

    def busy(self, start: str, end: str) -> list[str]:
        """Read-only: summaries of events overlapping ``[start, end)`` on the managed
        calendar (for a propose-time conflict heads-up). ``[]`` when nothing overlaps, the
        window isn't concrete, no backend is configured, or the read fails — it must never
        raise (a conflict check must not break the propose path)."""
        ...


class NullCalendarClient:
    """Default: no calendar configured — a confirmed hold declines gracefully."""

    def create_event(self, event: CalendarEvent, *, invite: bool = False) -> str:
        raise CalendarUnavailable("calendar backend not configured")

    def busy(self, start: str, end: str) -> list[str]:
        return []


class GoogleCalendarClient:
    """Creates a hold on a Google Calendar. ``service`` is a googleapiclient
    Calendar resource (injected in tests). Attendees are listed but never notified
    (``sendUpdates="none"``) — Nora does not contact third parties from a hold."""

    def __init__(self, calendar_id: str, *, service: Any) -> None:
        self._calendar_id = calendar_id or "primary"
        self._service = service

    def create_event(self, event: CalendarEvent, *, invite: bool = False) -> str:
        if not (_is_concrete(event.start) and _is_concrete(event.end)):
            # The propose path stores a placeholder when no time was given; decline
            # rather than book a hold with no real time (fail-closed, honest).
            raise CalendarUnavailable("no concrete time for the calendar hold")
        body = {
            "summary": event.summary,
            "start": {"dateTime": event.start},
            "end": {"dateTime": event.end},
            "attendees": [{"email": a} for a in event.attendees],
        }
        # invite=True sends the invitation to attendees (consent-gated); the event is
        # organized by THIS calendar (Nora's own labeled service-account calendar), never
        # the principal — so the invite comes from Nora, not as the CEO. Default: private.
        created = (
            self._service.events()
            .insert(
                calendarId=self._calendar_id, body=body,
                sendUpdates="all" if invite else "none",
            )
            .execute()
        )
        return str(created.get("id", ""))

    def busy(self, start: str, end: str) -> list[str]:
        if not (_is_concrete(start) and _is_concrete(end)):
            return []
        try:
            resp = (
                self._service.events()
                .list(
                    calendarId=self._calendar_id, timeMin=start, timeMax=end,
                    singleEvents=True, orderBy="startTime", maxResults=10,
                )
                .execute()
            )
        except Exception:  # noqa: BLE001 — a conflict check must never break the propose path
            return []
        out = []
        for ev in resp.get("items", []):
            if not isinstance(ev, dict) or ev.get("status") == "cancelled":
                continue
            if ev.get("transparency") == "transparent":  # "free"/availability events don't clash
                continue
            out.append(str(ev.get("summary", "(busy)")))
        return out


def build_google_calendar_service(credentials_json: str, subject: str | None = None) -> Any:
    """Build a Google Calendar API service from a service-account JSON key file path.
    Imported lazily so the google libs aren't required unless this backend is selected.

    ``subject`` domain-wide-delegates the SA to impersonate that mailbox (e.g.
    ``nora@demo…``), so ``primary`` resolves to her calendar and invites are organized
    from her own labeled identity. Without it the SA acts as itself — which has no
    usable calendar in a Workspace org, so the event insert fails. Mirrors the Gmail/
    Drive builders, which also impersonate the tenant user."""
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    creds = service_account.Credentials.from_service_account_file(
        credentials_json,
        scopes=["https://www.googleapis.com/auth/calendar.events"],
        subject=subject,
    )
    return build("calendar", "v3", credentials=creds, cache_discovery=False)
