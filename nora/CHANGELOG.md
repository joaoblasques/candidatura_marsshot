# Changelog

All notable changes to Nora are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/); versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added — Postmark provider wiring (productionization pilot, U2–U4)
- Postmark inbound adapter (`app/email/postmark_inbound.py`): maps the inbound
  webhook payload to the normalized shape (`MailboxHash` → correlation token,
  `OriginalRecipient` → secret-address local, `X-Spam-Tests: DKIM_VALID_AU` →
  DKIM-aligned).
- Webhook authenticity now uses HTTP Basic Auth (`verify_basic_auth`), replacing
  the v1.0 HMAC body-signature; `process_inbound` takes a provider-agnostic
  `adapt` callable so the gauntlet stays provider-neutral.
- `PostmarkSendClient` (`app/email/postmark_send.py`): real outbound send via
  `httpx`, replacing `LoggingProviderClient`. The send destination is resolved
  from the CEO registry, never the inbound `From` (KTD6).
- Application logging configured at INFO (`configure_logging`); `ceo_registry`,
  `secret_address_local`, and `reply_to` added to log redaction.

### Changed — configuration (breaking for any existing deployment)
- Renamed `NORA_PROVIDER_SIGNING_SECRET` → `NORA_WEBHOOK_BASIC_AUTH` (the webhook
  `user:password`); added `NORA_POSTMARK_SENDER` (verified sender-signature
  `From`). `NORA_PROVIDER_API_KEY` is the Postmark server token.
  `NORA_ADDRESS_DOMAIN` now defaults to `inbound.postmarkapp.com`. Update your
  secrets manager — `require_bootable()` refuses to start without the new
  variables.

### Added — v1.0 "Nora drafts, you send" trust loop
- Email-native trust loop: a CEO forwards a thread to a per-CEO secret address
  with a code (`WIYOW` to draft, `S` to summarize) or plain English; Nora replies
  with a plain-language receipt plus a copy-ready draft (or summary). **Nora never
  sends to a recipient** — the CEO always sends.
- Inbound webhook with verify-before-parse (HMAC), replay + rate-limit protection,
  and sender auth (per-CEO secret address + DKIM-verified `From`).
- Forwarded-thread + command parsing (Gmail/Apple/Outlook), deterministic intent
  routing, Claude forced-tool-call drafting, receipt composition (draft-at-top),
  and revise loop via a plus-addressed correlation token.
- `Dockerfile` + `deploy/README.md` (provider wiring, DNS/MX, SPF/DKIM/DMARC,
  shared-state and secrets requirements).
- Local end-to-end simulator (`scripts/simulate.py`) — offline, or real Claude
  when `ANTHROPIC_API_KEY` is set.
- CI (`ruff` + `pytest` on push/PR) and a gated real-model drafting eval
  (`uv run pytest -m eval`).

### Security
- Revise-token **owner-binding** (prevents cross-tenant thread access) and
  128-bit correlation tokens.
- Prompt-injection hardening: untrusted forwarded content is delimiter-escaped and
  the recipient anomaly check compares against the intended sender only.
- Per-CEO rate limiting, background failure receipts, and redaction of sensitive
  fields (including `reply_to`) from logs.

### Fixed
- Drafting prompt no longer leaks the task label (`DRAFT`/`SUMMARIZE`) into the
  draft body — caught via the real-model simulator, not unit tests. See
  `docs/solutions/2026-06-01-real-model-prompt-adherence.md`.

### Deferred to v2.0
- OAuth + Drafts-injection delivery, rich-text/attachment drafts, persistent
  voice/relationship memory, confidential (`C`) internal-only routing, and a
  shared/persistent state backend for multi-instance deployment.
