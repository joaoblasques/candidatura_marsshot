# Project rules — Nora

Instructions for any AI agent (Claude Code or otherwise) working in this repo. These
**override default agent behavior**. This file is meant to be self-sufficient: follow it
and you should produce work to this project's standard without further context.

## What Nora is (read first)

Nora is an **email-native AI executive assistant** — the "**Drafts, You Send**" trust loop.
A CEO forwards an email thread to their per-CEO secret Nora address with a command code
(`WIYOW` to draft a reply, `S` to summarize) or a plain-English request. Nora reads the
thread, drafts in the CEO's voice via an LLM, and emails back a plain-language **receipt**
plus a copy-ready **draft** or **summary**. The human sends it.

### 🔴 Cardinal rule — never send as the user (non-negotiable)

**Nora NEVER sends email to a third-party recipient.** She drafts; the human sends.
Outward actions (e.g. a meeting proposal) are **consent-gated**: proposed, then executed
only after the human's explicit confirmation (`AG`). This is the product's identity and its
safety model — not a setting. When writing code, tests, or config, never introduce a path
that could auto-send to a recipient, and never impersonate the CEO as the From identity to
an outside party. In dev/test, **nothing may touch real outbound mail** (see Production).

## Stack & architecture

Full stack, file map, and where things live: **`docs/architecture.md`**. `Roadmap.md` is the
actionable build backlog (checkboxes). `STRATEGY.md` is prose product context. `README.md` is
the public overview.

## How we build (TDD)

- **Test-first.** Write a failing test → minimal code to pass → refactor. Tests live in
  `tests/` mirroring `app/`. Design for testability: inject seams (fakes for services,
  file stores in `tmp_path`) rather than mocking internals — follow the patterns already in
  `tests/`.
- `asyncio_mode = "auto"` (no `@pytest.mark.asyncio` needed). Real-model **evals are excluded
  by default** (`-m eval`, cost tokens) — don't run or add them to the default gate.
- Match the surrounding code's style, naming, and comment density. `ruff` line length **100**.
- **DRY, YAGNI.** Don't add abstraction, config, or features beyond the unit's stated scope.

## Autonomy (standing authorization)

The operator delegates software-development decisions and wants minimal interruptions.
**Default to your recommended option and proceed — do not pause to ask.** This standing
authorization covers, end to end:

- **Design/brainstorm:** pick the approach; still write the design doc as the artifact
  (`docs/brainstorms/YYYY-MM-DD-<topic>.md`), but don't wait for sign-off.
- **Planning:** write the plan and proceed; don't wait for approval.
- **Implementation, code review, execution-mode choice:** choose and run.
- **Branch, push, mark a PR ready, and merge to `master`** when the unit is green.
- **Promote to the production Mac Mini** when a version is demo-ready (the promote
  script is test-gated and reversible) — then report what you did.

Keep the per-work-unit draft-PR cadence (below) so progress stays visible asynchronously —
that, not asking, is how the operator stays in the loop. When the backlog is empty, drive the
next unit from **`Roadmap.md`** (highest leverage first) rather than waiting for direction.

**Only stop and ask when:**
- a requirement is genuinely ambiguous and can't be resolved from the repo/context;
- a gate fails and you can't fix it;
- there's a real fork with no defensible default; or
- you would **send real email to an actual third party** on someone's behalf (Nora's
  live consent-gated send to real people) or take a **destructive/irreversible action
  with no rollback** — these reach outside parties and stay human-confirmed, mirroring
  Nora's own consent ethos.

Report what you did after autonomous work; don't ask before.

## Git workflow

- **Open a draft PR per work unit.** At the start of each logical unit, create a branch and
  open a **draft** PR early; keep it updated. Mark it **ready** only once the unit is complete
  and CI is green. Don't batch several units into one PR.
- **Never commit directly to `master`** — always branch first.
- **One focused commit per logical unit**, small descriptive message. Use **Conventional
  Commits**: `feat(scope): …`, `fix(scope): …`, `docs: …`, `chore: …` (see `git log`).
- **Watch CI to green before merging.** PRs run a `test` job (+ a Cloudflare Pages build);
  merge only when checks pass. Squash-merge and delete the branch.
- Push, marking ready, merge, and production promote are **pre-authorized** (see Autonomy).
  The remaining hard line is real third-party sends and unrecoverable actions.

## Keep user-facing docs in sync

When a unit changes commands or user-visible behavior, update **all three** together:
`app/codes.py` `HELP` text · `docs/command-codes.md` · `website/docs/` (e.g. `commands.md`).
A design doc alone does not count as "documented." Details: `docs/architecture.md`.

Documented solutions to past problems live in `docs/solutions/` — consult when implementing
or debugging in a documented area.

## Production & deploy

Production runs always-on on a Mac Mini; **MacBook is dev/test and never touches real mail**.
Promotion is punctual and test-gated via release tags. Full runbook: `docs/architecture.md`,
`deploy/mac-mini/README.md`.

## Secrets & safety

Secrets are **never committed** (this repo may be public). Credentials live in `.env` (gitignored)
and, in prod, in the Mini's own `.env`. Never print secret values into logs, commit messages, or
PR text.

## Definition of done — check before calling a unit finished

1. **New/changed behavior is covered by tests** (written test-first).
2. `uv run pytest -q` — **all green** (evals excluded by default is expected).
3. `uv run ruff check .` — **clean**.
4. **User-facing change?** The doc trio above is updated and `mkdocs build --strict` passes.
5. **Non-trivial unit?** A design doc exists under `docs/brainstorms/`.
6. PR was a draft during work, marked **ready only when CI is green**, then squash-merged.
7. You **reported what you did** (and, if promoted, that prod is healthy).
