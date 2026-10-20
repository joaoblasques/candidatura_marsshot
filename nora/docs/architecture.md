# Architecture & operations reference

Supplementary reference for Nora. `CLAUDE.md` holds the rules and safety model every
session must follow; this file holds the lookup detail — stack, file map, deploy, and
doc-sync mechanics. Read on demand, not by default.

## Stack & architecture

- **Python ≥ 3.12**, managed with **`uv`** (deps in `pyproject.toml` / `uv.lock`; venv `.venv`).
- **FastAPI + uvicorn** service; inbound email arrives as a webhook. **pydantic-settings**
  config (`app/config.py`, env prefix `NORA_`); settings load from `.env`.
- **LLM drafting**: Claude (paid) or OpenRouter (free) backends under `app/drafting/`.
- **Outbound**: Resend (prod, domain-DKIM), SMTP/Mailpit (local), Postmark — under `app/email/`.
- **Assurance rungs** (`app/posture.py`): **R1** cloud LLM · **R2** in-tenant drafts (mail stays
  in the client's Google/M365 tenant) · **R3** self-hosted local inference (zero egress).
- **State**: atomic JSON file stores (`app/state/`), not a database.

### Where things live

| Path | Responsibility |
|------|----------------|
| `app/main.py` | FastAPI app + inbound webhook entry point |
| `app/pipeline.py` | Core orchestration: interpret → draft → compose receipt → send to CEO |
| `app/interpreter.py`, `app/nl_interpreter.py`, `app/codes.py` | Command-code + natural-language parsing; `codes.py` also holds the `HELP` text |
| `app/config.py` | `Settings` (pydantic-settings, `NORA_*`) |
| `app/email/` | Inbound parsing + outbound send clients + Gmail OAuth |
| `app/mailbox/` | Connected-mailbox providers (Gmail, Graph) for inbox actions |
| `app/tenant.py` | In-tenant (R2) draft clients (Gmail/Graph) |
| `app/autodraft.py` | Ambient poller: watch a connected inbox → save reply drafts (never sends) |
| `app/receipt.py` | The CEO-facing receipt/reply composition |
| `scripts/` | Entrypoints + ops: `release.sh`, `promote.sh`, `smoke_prod.sh`, pollers, `gmail_oauth_login` |
| `deploy/` | Mac Mini LaunchAgents + Cloudflare tunnel/worker (`deploy/mac-mini/README.md`) |
| `tests/` | Mirrors `app/`; TDD |
| `docs/` | `brainstorms/` (design docs), `plans/`, `solutions/`, `strategy/` |
| `website/` | Dev docs (MkDocs → docs.nora-bennett.com) — **not** the marketing site |
| `marketing/` | Commercial site (Astro → GitHub Pages) — keep separate from `website/` |

`Roadmap.md` is the actionable build backlog (checkboxes). `STRATEGY.md` is prose product
context. `README.md` is the public overview.

## Production & deploy

Production runs always-on on a Mac Mini (`app.example.com`); the **MacBook is dev/test and
never touches real mail** (local email → Mailpit via `NORA_TRANSPORT=smtp`; see
`deploy/LOCAL_DEMO.md`). Promotion is punctual via release tags: `scripts/release.sh` (cut a
`prod-YYYY-MM-DD` tag on `master`, test-gated) → `scripts/promote.sh <tag>` on the Mini →
`scripts/smoke_prod.sh`. Full runbook: `deploy/mac-mini/README.md`.

## Keep user-facing docs in sync

When a unit changes commands or user-visible behavior, update **all three** together:
`app/codes.py` `HELP` text · `docs/command-codes.md` · `website/docs/` (e.g. `commands.md`).
A design doc alone does not count as "documented." The website must build clean:
`cd website && uv run --with mkdocs --with mkdocs-material mkdocs build --strict`.

## Documented solutions

`docs/solutions/` holds documented solutions to past problems (bugs, best practices, workflow
patterns), organized by category with YAML frontmatter (`module`, `tags`, `problem_type`).
Consult it when implementing or debugging in a documented area.
