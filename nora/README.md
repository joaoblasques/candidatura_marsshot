# Nora

Email-native AI executive assistant. **"Drafts, You Send" trust loop + consent-gated meeting proposals (0.2.0).**

A CEO forwards an email thread to their per-CEO secret Nora address with a command
code (`WIYOW` to draft a reply, `S` to summarize) or a plain-English request. Nora
reads the thread, drafts in the CEO's voice via Claude, and emails back a
plain-language **receipt** plus a clean, copy-ready **draft** (for `WIYOW`) or a
**summary** (for `S`). The CEO sends anything themselves — **Nora never sends to a
recipient.**

See `docs/plans/2026-05-29-001-feat-nora-drafts-you-send-v1-plan.md` for the
implementation plan and `STRATEGY.md` for product context.

## Develop

```bash
uv sync                      # install deps into .venv
uv run pytest                # run tests
uv run uvicorn app.main:app --reload   # run the service locally
```

## Configuration

Settings load from environment (see `app/config.py`). Required to boot:
`NORA_EXTERNAL_LLM_ACK=true` (operator acknowledgment that thread content is
processed by an external LLM in v1.0), plus the provider and Anthropic credentials.
Copy `.env.example` to `.env` for local development.
