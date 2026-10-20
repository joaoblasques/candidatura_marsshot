#!/usr/bin/env bash
# Rebuild nora/ from a clean git-archive snapshot of the private Nora repo (committed files only).
# Private inputs, never committed (fail closed if absent):
#   $PRIVATE_DIR/scrub.sed             sed -E expressions that replace third-party strings
#   $PRIVATE_DIR/mirror-forecast.patch removes the client integration (the patch itself contains it)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC="${NORA_SRC:-$HOME/Dev/AI/projects/Nora_Bennett}"
REV="${NORA_SRC_REV:-HEAD}"
OUT="${NORA_OUT:-$ROOT/nora}"
PRIVATE="${PRIVATE_DIR:-$ROOT/private}"

for f in scrub.sed mirror-forecast.patch; do
  [ -s "$PRIVATE/$f" ] || { echo "missing private input: $PRIVATE/$f" >&2; exit 2; }
done
case "$OUT" in ""|"/"|"$HOME") echo "refusing OUT=$OUT" >&2; exit 2;; esac

rm -rf "$OUT" && mkdir -p "$OUT"
git -C "$SRC" archive "$REV" | tar -x -C "$OUT"
SRC_COMMIT=$(git -C "$SRC" rev-parse --short "$REV")

cd "$OUT"
# drop-list: client code, marketing/ops material, agent tooling, anything naming third parties
rm -rf app/forecast tests/test_forecast_* tests/test_main_forecast.py tests/test_ff_* tests/test_agentics_* \
  tests/test_deep_review_workflow.py scripts/ff_* scripts/agentics marketing website deploy .claude \
  STRATEGY.md Roadmap.md HANDOFF.md .github/workflows/agentics-*.yml
find docs -mindepth 1 -maxdepth 1 ! -name architecture.md -exec rm -rf {} + 2>/dev/null || true

# scrub: third-party strings become placeholders (expressions live in private/)
find . -type f -print0 | xargs -0 grep -lI '' 2>/dev/null | while read -r f; do
  sed -i '' -E -f "$PRIVATE/scrub.sed" "$f"
done

# decouple the removed client integration (config fields, routes, inbound branch, tests)
patch -p1 -s < "$PRIVATE/mirror-forecast.patch"

# release path: ssh alias and runbook pointers
sed -i '' -E -e "s#ssh mini#ssh <prod-host>#g" -e 's#See deploy/mac-mini/README\.md\.#Deploy runbook not included in this mirror.#' \
  -e 's#See deploy/mac-mini/README\.md#Deploy runbook not included in this mirror#' scripts/promote.sh scripts/release.sh

cat > MIRROR.md <<MD
# Mirror of a private repository

Sanitized snapshot (no git history) of the private Nora repository at commit \`$SRC_COMMIT\`.
Rebuilt by \`scripts/build-mirror.sh\` in the parent repo; that needs the private source and two private
input files, so it cannot be rebuilt from a public checkout.

## Divergences from the private source
- Removed: client-specific integration code and tests, marketing and developer-docs sites, deployment
  configs, strategy and internal planning docs, agent tooling (\`.claude/\`).
- Patched (private patch, not published): client-specific config fields, webhook routing and inbound
  authentication branch removed from \`app/config.py\`, \`app/main.py\`, \`app/email/inbound.py\`, \`.env.example\`.
- Placeholders: third-party domains (\`example.nl\`), production URL (\`app.example.com\`), ssh alias
  (\`<prod-host>\`), personal email and paths.
- Some docs here (\`CLAUDE.md\`, \`README.md\`, \`docs/architecture.md\`, \`CHANGELOG.md\`) still point to removed
  files such as \`Roadmap.md\`, \`STRATEGY.md\`, \`deploy/\`, \`website/\`; those links do not resolve in this mirror.
MD
echo "mirror built from $SRC_COMMIT"
