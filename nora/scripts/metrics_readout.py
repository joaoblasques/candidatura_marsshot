"""Offline weekly metrics readout for Nora R1 (U5).

Parses ``event=metric`` lines out of the app log and prints an honest weekly
summary — passive signals, the implicit quality proxies, and the consent-gated
**action loop** (proposed → confirmed → executed: confirmation and execution rates,
a primary STRATEGY metric). No DB, no network, no dashboard. Run on the prod host
against its log:

    uv run python -m scripts.metrics_readout ~/Library/Logs/nora-app.log

Metrics are *computed here* from raw events (emit-raw, compute-in-readout), so
proxy definitions and the acceptance window can change without re-instrumenting.
With only a couple of live users these are directional gut-checks, not statistics —
the summary states sample size and labels proxies as proxies.
"""

from __future__ import annotations

import argparse
import json
import statistics
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime


def parse_events(lines: list[str]) -> list[dict]:
    """Extract metric-event dicts from log lines; ignore everything else."""
    events: list[dict] = []
    for line in lines:
        marker = line.find("metric {")
        if marker == -1:
            continue
        try:
            obj = json.loads(line[marker + len("metric ") :])
        except (ValueError, TypeError):
            continue
        if isinstance(obj, dict) and obj.get("event") == "metric":
            events.append(obj)
    return events


def iso_week(ts: str) -> str | None:
    """'YYYY-Www' for an ISO timestamp, or None if unparseable."""
    try:
        dt = datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None
    y, w, _ = dt.isocalendar()
    return f"{y}-W{w:02d}"


@dataclass
class WeekSummary:
    week: str
    ceos: set[str] = field(default_factory=set)
    inbound_outcomes: Counter = field(default_factory=Counter)
    codes: Counter = field(default_factory=Counter)
    draft_total: int = 0
    nl_fallback: int = 0
    originals: int = 0          # draft, revise=False, DRAFT
    revisions: int = 0          # draft, revise=True
    edit_depths: list[int] = field(default_factory=list)
    clarifying: int = 0
    result_kinds: Counter = field(default_factory=Counter)
    latencies: list[int] = field(default_factory=list)
    thread_hits: Counter = field(default_factory=Counter)  # thread_key -> accepted count
    action_phases: Counter = field(default_factory=Counter)  # consent-gated action loop phases

    # Phases that count as a successful execution of a confirmed action, and as a failure.
    _EXECUTED_PHASES = ("sent", "calendar_held")
    _FAILED_PHASES = ("failed", "calendar_unavailable", "stranded_not_resent")

    @property
    def volume(self) -> int:
        return self.inbound_outcomes.get("accepted", 0)

    @property
    def proposed(self) -> int:
        return self.action_phases.get("proposed", 0)

    @property
    def confirmed(self) -> int:
        return self.action_phases.get("confirmed", 0)

    @property
    def declined(self) -> int:
        return self.action_phases.get("declined", 0)

    @property
    def executed(self) -> int:
        return sum(self.action_phases.get(p, 0) for p in self._EXECUTED_PHASES)

    @property
    def action_failures(self) -> int:
        return sum(self.action_phases.get(p, 0) for p in self._FAILED_PHASES)

    @property
    def confirmation_rate(self) -> float | None:
        """Share of proposed actions the CEO confirmed (primary action-loop metric)."""
        return (self.confirmed / self.proposed) if self.proposed else None

    @property
    def execution_rate(self) -> float | None:
        """Share of confirmed actions Nora then executed successfully."""
        return (self.executed / self.confirmed) if self.confirmed else None

    @property
    def revise_rate(self) -> float | None:
        return (self.revisions / self.originals) if self.originals else None

    @property
    def acceptance_proxy(self) -> float | None:
        rr = self.revise_rate
        return None if rr is None else max(0.0, 1.0 - min(1.0, rr))

    @property
    def reforwards(self) -> int:
        return sum(c - 1 for c in self.thread_hits.values() if c > 1)


def summarize(events: list[dict]) -> dict[str, WeekSummary]:
    weeks: dict[str, WeekSummary] = {}
    for e in events:
        wk = iso_week(str(e.get("ts", "")))
        if wk is None:
            continue
        w = weeks.setdefault(wk, WeekSummary(week=wk))
        kind = e.get("kind")
        if kind == "inbound":
            outcome = str(e.get("outcome", "unknown"))
            w.inbound_outcomes[outcome] += 1
            if outcome == "accepted":
                if e.get("ceo_hash"):
                    w.ceos.add(str(e["ceo_hash"]))
                if e.get("thread_key"):
                    w.thread_hits[str(e["thread_key"])] += 1
        elif kind == "draft":
            w.draft_total += 1
            w.result_kinds[str(e.get("result_kind", "?"))] += 1
            if e.get("action_code"):
                w.codes[str(e["action_code"])] += 1
            if e.get("nl_fallback"):
                w.nl_fallback += 1
            if e.get("clarifying"):
                w.clarifying += 1
            if e.get("revise"):
                w.revisions += 1
                w.edit_depths.append(int(e.get("revision_count", 0)))
            elif e.get("result_kind") == "DRAFT":
                w.originals += 1
        elif kind == "send" and isinstance(e.get("latency_ms"), int):
            w.latencies.append(e["latency_ms"])
        elif kind == "action":
            w.action_phases[str(e.get("phase", "?"))] += 1
    return weeks


def _pct(n: int, d: int) -> str:
    return f"{(100 * n / d):.0f}%" if d else "n/a"


def format_summary(weeks: dict[str, WeekSummary]) -> str:
    if not weeks:
        return "No metric events found.\n"
    lines: list[str] = ["Nora R1 — weekly metrics (proxies are directional; low N)"]
    prev_ceos: set[str] = set()
    for wk in sorted(weeks):
        w = weeks[wk]
        returning = len(w.ceos & prev_ceos)
        lines.append(f"\n== {wk} ==")
        lines.append(f"  Active CEOs: {len(w.ceos)} (returning from prior wk: {returning})")
        drops = {k: v for k, v in w.inbound_outcomes.items() if k != "accepted"}
        lines.append(f"  Volume (accepted): {w.volume}   drops: {dict(drops) or 'none'}")
        lines.append(
            f"  Codes: {dict(w.codes) or 'none'}   NL-fallback: "
            f"{_pct(w.nl_fallback, w.draft_total)}"
        )
        lines.append(f"  Drafts: {w.draft_total}   kinds: {dict(w.result_kinds) or 'none'}")
        if w.acceptance_proxy is not None:
            depth = f"{statistics.mean(w.edit_depths):.1f}" if w.edit_depths else "0"
            lines.append(
                f"  Acceptance proxy (1 - revise-rate): {w.acceptance_proxy:.0%} "
                f"(revisions {w.revisions} / originals {w.originals}; mean edit-depth {depth}) [proxy]"
            )
        else:
            lines.append("  Acceptance proxy: n/a (no original drafts) [proxy]")
        lines.append(
            f"  Interpretation: clarifying {_pct(w.clarifying, w.draft_total)}, "
            f"re-forwards {w.reforwards} [proxy]"
        )
        if w.proposed or w.confirmed:
            conf = f"{w.confirmation_rate:.0%}" if w.confirmation_rate is not None else "n/a"
            exe = f"{w.execution_rate:.0%}" if w.execution_rate is not None else "n/a"
            lines.append(
                f"  Action loop: proposed {w.proposed} → confirmed {w.confirmed} "
                f"(confirmation {conf}), declined {w.declined}; executed {w.executed}/"
                f"{w.confirmed} (execution {exe}), failures {w.action_failures}"
            )
        if w.latencies:
            p50 = int(statistics.median(w.latencies))
            p90 = int(sorted(w.latencies)[min(len(w.latencies) - 1, int(0.9 * len(w.latencies)))])
            lines.append(f"  Latency (send): p50 {p50}ms, p90 {p90}ms, n={len(w.latencies)}")
        prev_ceos = w.ceos
    total_events = sum(
        w.draft_total + sum(w.inbound_outcomes.values()) + len(w.latencies)
        + sum(w.action_phases.values())
        for w in weeks.values()
    )
    lines.append(f"\nSample: {total_events} events across {len(weeks)} week(s). Directional only.\n")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Nora R1 weekly metrics readout")
    parser.add_argument(
        "log", nargs="?", default="~/Library/Logs/nora-app.log", help="path to nora-app.log"
    )
    args = parser.parse_args(argv)
    import os

    path = os.path.expanduser(args.log)
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError as exc:
        print(f"Could not read log: {exc}")
        return 1
    print(format_summary(summarize(parse_events(lines))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
