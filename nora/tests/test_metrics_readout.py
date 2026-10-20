"""U5 — offline weekly readout. Hermetic: builds a fixture log, never the prod log."""

from __future__ import annotations

import json

from scripts.metrics_readout import format_summary, iso_week, parse_events, summarize

WK = "2026-06-08T09:00:00+00:00"  # ISO week 2026-W24


def _line(**fields) -> str:
    fields.setdefault("event", "metric")
    fields.setdefault("ts", WK)
    return f"2026-06-08 09:00:00 INFO app.metrics metric {json.dumps(fields)}"


def _fixture() -> list[str]:
    return [
        "2026-06-08 09:00:00 INFO app.main some unrelated log line",  # ignored
        "garbage metric {not json",  # malformed → ignored
        _line(kind="inbound", outcome="accepted", ceo_hash="ceoA", thread_key="t1"),
        _line(kind="inbound", outcome="accepted", ceo_hash="ceoA", thread_key="t1"),  # re-forward
        _line(kind="inbound", outcome="accepted", ceo_hash="ceoB", thread_key="t2"),
        _line(kind="inbound", outcome="unauthorized", ceo_hash="ceoX"),
        _line(kind="draft", result_kind="DRAFT", action_code="WIYOW", revise=False, nl_fallback=False),
        _line(kind="draft", result_kind="DRAFT", action_code="WIYOW", revise=False, nl_fallback=False),
        _line(kind="draft", result_kind="DRAFT", revise=True, revision_count=1),  # a revision
        _line(kind="draft", result_kind="CLARIFY", clarifying=True, nl_fallback=True),
        _line(kind="draft", result_kind="SUMMARY", action_code="S", revise=False),
        _line(kind="send", outcome="sent", latency_ms=2000),
        _line(kind="send", outcome="sent", latency_ms=4000),
        # consent-gated action loop: 3 proposed, 2 confirmed, 1 declined, then executed
        # as 1 email send + 1 calendar hold (2 executed), 0 failures.
        _line(kind="action", phase="proposed", ceo_hash="ceoA"),
        _line(kind="action", phase="proposed", ceo_hash="ceoA"),
        _line(kind="action", phase="proposed", ceo_hash="ceoB"),
        _line(kind="action", phase="confirmed", ceo_hash="ceoA"),
        _line(kind="action", phase="confirmed", ceo_hash="ceoB"),
        _line(kind="action", phase="declined", ceo_hash="ceoA"),
        _line(kind="action", phase="sent", ceo_hash="ceoA"),
        _line(kind="action", phase="calendar_held", ceo_hash="ceoB"),
    ]


def test_parse_ignores_non_metric_and_malformed() -> None:
    events = parse_events(_fixture())
    assert all(e["event"] == "metric" for e in events)
    assert len(events) == 19  # 21 lines − 1 unrelated − 1 malformed


def test_iso_week() -> None:
    assert iso_week(WK) == "2026-W24"
    assert iso_week("not-a-date") is None


def test_volume_codes_and_nl_fallback() -> None:
    w = summarize(parse_events(_fixture()))["2026-W24"]
    assert w.volume == 3  # three accepted
    assert w.inbound_outcomes["unauthorized"] == 1
    assert w.codes["WIYOW"] == 2
    assert w.draft_total == 5
    assert w.nl_fallback == 1


def test_acceptance_proxy_from_revise_rate() -> None:
    w = summarize(parse_events(_fixture()))["2026-W24"]
    assert w.originals == 2  # two revise=False DRAFTs (SUMMARY isn't a DRAFT original)
    assert w.revisions == 1
    assert w.revise_rate == 0.5
    assert w.acceptance_proxy == 0.5


def test_reforward_detection_and_clarifying() -> None:
    w = summarize(parse_events(_fixture()))["2026-W24"]
    assert w.reforwards == 1  # t1 seen twice
    assert w.clarifying == 1


def test_active_and_returning_ceos() -> None:
    w = summarize(parse_events(_fixture()))["2026-W24"]
    assert w.ceos == {"ceoA", "ceoB"}  # only accepted inbound counts; ceoX was unauthorized


def test_latency_collected() -> None:
    w = summarize(parse_events(_fixture()))["2026-W24"]
    assert sorted(w.latencies) == [2000, 4000]


def test_action_loop_counts_and_rates() -> None:
    w = summarize(parse_events(_fixture()))["2026-W24"]
    assert w.proposed == 3
    assert w.confirmed == 2
    assert w.declined == 1
    assert w.executed == 2  # 1 sent + 1 calendar_held
    assert w.action_failures == 0
    assert w.confirmation_rate == 2 / 3  # confirmed / proposed
    assert w.execution_rate == 1.0       # executed / confirmed


def test_action_rates_none_without_proposals_or_confirms() -> None:
    w = summarize(parse_events([_line(kind="draft", result_kind="DRAFT", revise=False)]))["2026-W24"]
    assert w.confirmation_rate is None  # no proposals
    assert w.execution_rate is None     # no confirms


def test_action_failures_counted() -> None:
    lines = [
        _line(kind="action", phase="proposed", ceo_hash="ceoA"),
        _line(kind="action", phase="confirmed", ceo_hash="ceoA"),
        _line(kind="action", phase="failed", ceo_hash="ceoA"),
        _line(kind="action", phase="calendar_unavailable", ceo_hash="ceoA"),
        _line(kind="action", phase="stranded_not_resent", ceo_hash="ceoA"),
    ]
    w = summarize(parse_events(lines))["2026-W24"]
    assert w.action_failures == 3  # failed + calendar_unavailable + stranded_not_resent
    assert w.executed == 0
    assert w.execution_rate == 0.0  # 0 executed / 1 confirmed


def test_format_includes_action_loop() -> None:
    out = format_summary(summarize(parse_events(_fixture())))
    assert "Action loop" in out
    assert "confirmation" in out.lower() and "execution" in out.lower()


def test_format_states_sample_size_and_handles_empty() -> None:
    out = format_summary(summarize(parse_events(_fixture())))
    assert "2026-W24" in out
    assert "Sample:" in out and "Directional" in out
    assert "Acceptance proxy" in out and "[proxy]" in out
    assert format_summary({}) == "No metric events found.\n"
