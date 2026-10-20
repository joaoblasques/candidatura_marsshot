# Mirror of a private repository

Sanitized snapshot (no git history) of the private Nora repository at commit `e985bc8`.
Rebuilt by `scripts/build-mirror.sh` in the parent repo; that needs the private source and two private
input files, so it cannot be rebuilt from a public checkout.

## Divergences from the private source
- Removed: client-specific integration code and tests, marketing and developer-docs sites, deployment
  configs, strategy and internal planning docs, agent tooling (`.claude/`).
- Patched (private patch, not published): client-specific config fields, webhook routing and inbound
  authentication branch removed from `app/config.py`, `app/main.py`, `app/email/inbound.py`, `.env.example`.
- Placeholders: third-party domains (`example.nl`), production URL (`app.example.com`), ssh alias
  (`<prod-host>`), personal email and paths.
- Some docs here (`CLAUDE.md`, `README.md`, `docs/architecture.md`, `CHANGELOG.md`) still point to removed
  files such as `Roadmap.md`, `STRATEGY.md`, `deploy/`, `website/`; those links do not resolve in this mirror.
