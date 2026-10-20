from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.calendar import (
    CalendarEvent,
    CalendarUnavailable,
    GoogleCalendarClient,
    NullCalendarClient,
    parse_slot,
)

_NOW = datetime(2026, 6, 17, 9, 0, tzinfo=timezone.utc)


def test_event_model() -> None:
    e = CalendarEvent(summary="Sync", start="2026-06-23T15:00:00Z",
                      end="2026-06-23T15:30:00Z", attendees=["sarah@acme.com"])
    assert e.summary == "Sync" and e.attendees == ["sarah@acme.com"]


def test_null_client_declines() -> None:
    with pytest.raises(CalendarUnavailable):
        NullCalendarClient().create_event(
            CalendarEvent(summary="x", start="s", end="e", attendees=[])
        )


def test_parse_slot_absolute_datetime() -> None:
    slot = parse_slot("let's meet on 2026-07-01 15:00", now=_NOW)
    assert slot is not None
    start, end = slot
    assert start.startswith("2026-07-01T15:00")
    assert end.startswith("2026-07-01T15:30")  # default 30-min duration


def test_parse_slot_returns_none_when_no_date() -> None:
    assert parse_slot("please set something up sometime", now=_NOW) is None
    assert parse_slot("", now=_NOW) is None


class _FakeEvents:
    def __init__(self, sink: dict) -> None:
        self._sink = sink

    def insert(self, *, calendarId, body, sendUpdates):  # noqa: N803
        self._sink.update(calendarId=calendarId, body=body, sendUpdates=sendUpdates)
        return self

    def execute(self):  # noqa: ANN201
        return {"id": "evt-123"}


class _FakeService:
    def __init__(self) -> None:
        self.sink: dict = {}

    def events(self):  # noqa: ANN201
        return _FakeEvents(self.sink)


def test_google_client_creates_event_with_concrete_times() -> None:
    svc = _FakeService()
    client = GoogleCalendarClient("primary", service=svc)
    eid = client.create_event(
        CalendarEvent(summary="Sync", start="2026-07-01T15:00:00+00:00",
                      end="2026-07-01T15:30:00+00:00", attendees=["sarah@acme.com"])
    )
    assert eid == "evt-123"
    assert svc.sink["calendarId"] == "primary"
    assert svc.sink["sendUpdates"] == "none"  # never notifies the third party
    assert svc.sink["body"]["start"]["dateTime"] == "2026-07-01T15:00:00+00:00"
    assert svc.sink["body"]["attendees"] == [{"email": "sarah@acme.com"}]


def test_google_client_declines_placeholder_time() -> None:
    client = GoogleCalendarClient("primary", service=_FakeService())
    with pytest.raises(CalendarUnavailable):
        client.create_event(
            CalendarEvent(summary="x", start="(time to be confirmed)", end="(time to be confirmed)")
        )


# --- conflict-aware scheduling: busy() read ------------------------------------


class _FakeListEvents:
    def __init__(self, items: list) -> None:
        self._items = items
        self.captured: dict = {}

    def list(self, **kw):  # noqa: ANN201
        self.captured.update(kw)
        return self

    def execute(self):  # noqa: ANN201
        return {"items": self._items}


class _FakeListService:
    def __init__(self, items: list) -> None:
        self.ev = _FakeListEvents(items)

    def events(self):  # noqa: ANN201
        return self.ev


_S, _E = "2026-07-01T15:00:00+00:00", "2026-07-01T15:30:00+00:00"


def test_busy_returns_overlapping_summaries_skipping_cancelled_and_free() -> None:
    svc = _FakeListService([
        {"summary": "Board call", "status": "confirmed"},
        {"summary": "Cancelled thing", "status": "cancelled"},
        {"summary": "Focus (free)", "transparency": "transparent"},
        {"summary": "1:1"},
    ])
    busy = GoogleCalendarClient("primary", service=svc).busy(_S, _E)
    assert busy == ["Board call", "1:1"]
    # The query is bounded to the proposed window.
    assert svc.ev.captured["timeMin"] == _S and svc.ev.captured["timeMax"] == _E
    assert svc.ev.captured["singleEvents"] is True


def test_busy_non_concrete_window_is_empty() -> None:
    assert GoogleCalendarClient("primary", service=_FakeListService([])).busy(
        "(time to be confirmed)", "(time to be confirmed)") == []


def test_busy_swallows_api_error() -> None:
    class _Boom:
        def events(self):  # noqa: ANN201
            raise RuntimeError("api down")

    assert GoogleCalendarClient("primary", service=_Boom()).busy(_S, _E) == []


def test_null_client_busy_is_empty() -> None:
    assert NullCalendarClient().busy(_S, _E) == []


def test_parse_slot_resolves_in_given_timezone_deterministically() -> None:
    # Absolute time resolves in the requested tz regardless of host TZ.
    utc = parse_slot("meet on 2026-07-01 15:00", now=_NOW, tz="UTC")
    ams = parse_slot("meet on 2026-07-01 15:00", now=_NOW, tz="Europe/Amsterdam")
    assert utc is not None and utc[0].endswith("+00:00")
    assert ams is not None and ams[0].endswith("+02:00")  # CEST in July


def test_google_client_invites_participants_when_requested() -> None:
    svc = _FakeService()
    client = GoogleCalendarClient("primary", service=svc)
    client.create_event(
        CalendarEvent(summary="Sync", start="2026-07-01T15:00:00+00:00",
                      end="2026-07-01T15:30:00+00:00", attendees=["sarah@acme.com"]),
        invite=True,
    )
    assert svc.sink["sendUpdates"] == "all"  # invitation is sent to attendees


def test_google_client_default_is_private_hold() -> None:
    svc = _FakeService()
    GoogleCalendarClient("primary", service=svc).create_event(
        CalendarEvent(summary="Sync", start="2026-07-01T15:00:00+00:00",
                      end="2026-07-01T15:30:00+00:00", attendees=["sarah@acme.com"])
    )
    assert svc.sink["sendUpdates"] == "none"  # private hold, nobody notified


def test_build_service_passes_subject_for_dwd_impersonation(monkeypatch) -> None:
    # The SA must impersonate the tenant mailbox (subject=) or `primary` resolves to the
    # SA's own (absent) calendar and the insert fails. Capture the kwargs passed to the
    # credentials factory.
    import app.calendar as cal
    from google.oauth2 import service_account
    from googleapiclient import discovery

    captured: dict = {}

    def _fake_from_file(path, scopes, subject):
        captured.update(path=path, scopes=scopes, subject=subject)
        return object()

    monkeypatch.setattr(
        service_account.Credentials, "from_service_account_file", staticmethod(_fake_from_file)
    )
    monkeypatch.setattr(discovery, "build", lambda *a, **k: "SERVICE")
    out = cal.build_google_calendar_service("/k.json", subject="nora@demo.nora-bennett.com")
    assert out == "SERVICE"
    assert captured["subject"] == "nora@demo.nora-bennett.com"
    assert captured["scopes"] == ["https://www.googleapis.com/auth/calendar.events"]
