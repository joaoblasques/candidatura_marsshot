"""Seed the showcase Google Drive with a realistic, demo-ready file corpus.

Materializes ~two dozen files (earnings, decks, contracts, finance, product, HR, notes)
into Nora's showcase Drive so the "find the right file among many" demo lands — including
four quarterly earnings files so "earnings" is ambiguous (shows disambiguation) while
"Q4 earnings" resolves to one. Office files are produced via the existing report renderer;
text files are written directly. Uploads via the existing GoogleDriveFileSource.create.

    NORA_FILE_SOURCE_BACKEND=gdrive NORA_GMAIL_CREDENTIALS_JSON=... NORA_GMAIL_TENANT_USER=... \
      uv run python -m scripts.seed_showcase_drive            # upload
    uv run python -m scripts.seed_showcase_drive --list       # just print the planned corpus

Idempotency note: Drive allows duplicate names; re-running creates duplicates. Run once,
or clear the Drive folder first.
"""

from __future__ import annotations

import sys

from app.reports.render import render_report

# (filename, format, body). format: "txt"/"csv"/"md" -> written as-is; "W"/"X" -> rendered
# to .docx/.xlsx via the report renderer. Bodies are short but representative — enough for
# Nora to read and summarize convincingly.
CORPUS: list[tuple[str, str, str]] = [
    ("Q1-2026-earnings.txt", "txt",
     "Q1 2026 Earnings\nRevenue: $4.2M (+12% YoY)\nNet income: $0.6M\nKey driver: new mid-market logos."),
    ("Q2-2026-earnings.txt", "txt",
     "Q2 2026 Earnings\nRevenue: $4.8M (+15% YoY)\nNet income: $0.7M\nGross margin improved to 71%."),
    ("Q3-2026-earnings.txt", "txt",
     "Q3 2026 Earnings\nRevenue: $5.3M (+16% YoY)\nNet income: $0.8M\nChurn down to 1.4% monthly."),
    ("Q4-2026-earnings.docx", "W",
     "Q4 2026 Earnings\n\nRevenue: $6.1M (+18% YoY), a record quarter.\n\nNet income: $1.0M; gross "
     "margin 73%.\n\nHighlights: closed the Brightwave partnership pilot; ARR crossed $22M; net revenue "
     "retention 118%.\n\nOutlook: guiding $26M ARR for FY2027."),
    ("FY2026-annual-report.docx", "W",
     "FY2026 Annual Report\n\nFull-year revenue $20.4M (+15%). Profitable in H2. Headcount 84.\n\n"
     "Strategy: move up-market; deepen the partner channel (Brightwave, others)."),
    ("budget-2026.xlsx", "X",
     "Category\tPlan\tActual\nR&D\t6.0M\t5.8M\nS&M\t4.5M\t4.7M\nG&A\t2.0M\t1.9M"),
    ("expenses-Q3.xlsx", "X",
     "Vendor\tAmount\nCloud\t82000\nTravel\t21000\nTooling\t15500"),
    ("Brightwave-partnership-contract.txt", "txt",
     "Mutual Partnership Agreement between the Company and Brightwave Inc. Term: 24 months. "
     "Scope: co-selling + a Q3 integration pilot. Primary contact: Sarah Mercer (Brightwave)."),
    ("NDA-template.txt", "txt", "Mutual Non-Disclosure Agreement template. Confidential information..."),
    ("vendor-contracts-summary.txt", "txt", "Summary of active vendor contracts and renewal dates."),
    ("2026-board-deck.docx", "W",
     "2026 Board Deck\n\nNorth star: become the trusted email-native executive assistant.\n\n"
     "Bets: assurance ladder (R1->R2->R3); partner channel; voice-fidelity drafting."),
    ("Q3-strategy-deck.docx", "W",
     "Q3 Strategy\n\nFocus: land the Brightwave partnership; ship in-tenant drafting (R2)."),
    ("product-one-pager.txt", "txt",
     "Nora Bennett: an email-native AI executive assistant. Drafts you send; consent-gated actions."),
    ("roadmap-2026.txt", "txt", "Roadmap: calendar invites, in-tenant drafting, file access/share, memory."),
    ("pricing-2026.txt", "txt", "Pricing by assurance rung: R1 standard, R2 in-tenant premium, R3 enterprise."),
    ("competitor-analysis.txt", "txt", "Competitor landscape: generic AI inboxes vs. Nora's consent-gated EA."),
    ("org-chart.txt", "txt", "CEO -> VP Eng, VP Sales, VP Product. 84 people across 6 teams."),
    ("hiring-plan-2026.txt", "txt", "2026 hiring plan: +6 eng, +4 sales, +2 product."),
    ("customer-list.csv", "csv", "name,plan,arr\nBrightwave,Premium,120000\nBeta Co,Standard,48000"),
    ("leadership-sync-notes.txt", "txt", "Leadership sync: prioritize Brightwave pilot; firm up Q4 close."),
    ("brand-guidelines.txt", "txt", "Brand: calm, trustworthy, precise. Voice: an excellent human EA."),
    ("press-release-draft.txt", "txt", "DRAFT press release: Company announces the Brightwave partnership."),
    ("security-overview.txt", "txt", "Security overview: consent-gated actions, per-rung data boundary, audit."),
    ("travel-itinerary.txt", "txt", "Itinerary: SF -> NYC, partner meetings, return Friday."),
]


def build_seed_corpus() -> list[tuple[str, bytes, str]]:
    """Materialize CORPUS into (filename, content_bytes, content_type). Deterministic."""
    out: list[tuple[str, bytes, str]] = []
    for name, fmt, body in CORPUS:
        if fmt in ("txt", "csv", "md"):
            ctype = "text/csv" if fmt == "csv" else "text/plain"
            out.append((name, body.encode("utf-8"), ctype))
        else:  # "W"/"X"/"PPT" -> Office file via the report renderer
            att = render_report(body, fmt)
            out.append((name, att.content, att.content_type))
    return out


def main(argv: list[str]) -> int:
    corpus = build_seed_corpus()
    if "--list" in argv:
        for name, content, ctype in corpus:
            print(f"  {name:32}  {ctype:20}  {len(content):>7} bytes")
        print(f"\n{len(corpus)} files planned.")
        return 0

    from app.config import get_settings
    from app.filesource import GoogleDriveFileSource, build_drive_write_service

    s = get_settings()
    if s.file_source_backend != "gdrive" or not (s.gmail_credentials_json and s.gmail_tenant_user):
        print("FAIL: set NORA_FILE_SOURCE_BACKEND=gdrive + NORA_GMAIL_CREDENTIALS_JSON + "
              "NORA_GMAIL_TENANT_USER (the showcase Drive credentials).")
        return 2
    # Seeding writes files, so it needs the full `drive` scope (the runtime path is
    # read-only). The SA's domain-wide delegation must include `.../auth/drive`.
    service = build_drive_write_service(s.gmail_credentials_json, s.gmail_tenant_user)
    drive = GoogleDriveFileSource(service=service)
    created = 0
    for name, content, ctype in corpus:
        link = drive.create(name, content, ctype)
        print(f"  {'ok ' if link else 'FAIL'}  {name}  {link or ''}")
        created += 1 if link else 0
    print(f"\nSeeded {created}/{len(corpus)} files into the showcase Drive.")
    return 0 if created == len(corpus) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
