#!/usr/bin/env bash
# Cut a production release tag (prod-YYYY-MM-DD[-N]) on a clean, in-sync master.
# Promote it on the Mac Mini with: scripts/promote.sh <tag>
# Deploy runbook not included in this mirror.
set -euo pipefail

DRY_RUN=0
[ "${1:-}" = "--dry-run" ] && DRY_RUN=1

cd "$(git rev-parse --show-toplevel)"

branch=$(git rev-parse --abbrev-ref HEAD)
if [ "$branch" != "master" ]; then
  echo "ERROR: on '$branch', not master — release tags are cut from master." >&2
  exit 1
fi
if [ -n "$(git status --porcelain)" ]; then
  echo "ERROR: working tree is dirty. Commit or stash first." >&2
  exit 1
fi
git fetch --quiet origin master
if [ "$(git rev-parse HEAD)" != "$(git rev-parse origin/master)" ]; then
  echo "ERROR: local master is not in sync with origin/master (pull/push first)." >&2
  exit 1
fi

base="prod-$(date +%Y-%m-%d)"
tag="$base"
n=2
while git rev-parse -q --verify "refs/tags/$tag" >/dev/null; do
  tag="${base}-${n}"
  n=$((n + 1))
done

if [ "$DRY_RUN" = 1 ]; then
  echo "[dry-run] would create + push tag '$tag' at $(git rev-parse --short HEAD)"
  exit 0
fi

# Local verification gate (KTD): nothing earns a release tag — and so nothing can be promoted
# to the Mini — unless the FULL dev-machine suite is green here first. This is what tests every
# change (including anything the agentics self-dev helper merged) on the local machine before it
# can reach prod. The Mini's promote.sh re-runs tests too (defence in depth); this catches
# problems on the dev box first. Emergency override (use sparingly): NORA_RELEASE_SKIP_TESTS=1.
if [ "${NORA_RELEASE_SKIP_TESTS:-0}" = "1" ]; then
  echo "WARNING: NORA_RELEASE_SKIP_TESTS=1 — skipping the local test gate (emergency override)." >&2
else
  echo "== Local verification before release (pytest + ruff) =="
  uv run pytest -q
  uv run ruff check .
  echo "== Local checks PASS — cutting the release =="
fi

git tag -a "$tag" -m "Production release $tag"
git push origin "$tag"
echo "Created and pushed $tag"
echo "Promote on the Mac Mini with:  scripts/promote.sh $tag"
