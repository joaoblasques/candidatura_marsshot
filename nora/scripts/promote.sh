#!/usr/bin/env bash
# Promote (or roll back) the local Nora production instance to a git tag.
#
#   scripts/promote.sh [<tag>] [--dry-run]
#
# Steps: checkout tag -> uv sync -> unit tests (ABORT on failure, no restart) ->
# restart LaunchAgents -> smoke test. Rollback = run again with a previous tag.
# Intended to run ON the Mac Mini. Deploy runbook not included in this mirror.
set -euo pipefail

# A non-interactive shell (e.g. `ssh <prod-host> 'scripts/promote.sh ...'`) does not source
# the login profile, so ~/.local/bin (where uv and cloudflared are installed per the
# runbook) is off PATH and `uv sync` fails with "uv: command not found". Add it here so
# the script works the same whether run on the box or over SSH.
export PATH="$HOME/.local/bin:$PATH"

DRY_RUN=0
tag=""
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    *) tag="$arg" ;;
  esac
done

cd "$(git rev-parse --show-toplevel)"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ -n "$(git status --porcelain)" ]; then
  echo "ERROR: working tree is dirty; refusing to promote (would lose local changes)." >&2
  exit 1
fi
git fetch --quiet --tags origin

if [ -z "$tag" ]; then
  tag=$(git tag --list 'prod-*' --sort=-creatordate | head -1)
  [ -n "$tag" ] || { echo "ERROR: no prod-* tag found and none given." >&2; exit 1; }
  echo "No tag given; using latest: $tag"
fi
git rev-parse -q --verify "refs/tags/$tag" >/dev/null \
  || { echo "ERROR: tag '$tag' not found (try: git fetch --tags)." >&2; exit 1; }

run() { echo "+ $*"; [ "$DRY_RUN" = 1 ] || "$@"; }

echo "== Promoting to $tag =="
run git checkout --quiet "$tag"
run uv sync --frozen

echo "== Unit tests (promotion aborts on failure; the gated real-model eval is excluded) =="
if [ "$DRY_RUN" = 0 ]; then
  uv run pytest -q || {
    echo "ERROR: tests failed — aborting promotion. Production is unchanged." >&2
    exit 1
  }
else
  echo "+ uv run pytest -q"
fi

echo "== Restarting LaunchAgents =="
uid=$(id -u)
for svc in com.nora.app com.nora.tunnel; do
  if launchctl print "gui/$uid/$svc" >/dev/null 2>&1; then
    run launchctl kickstart -k "gui/$uid/$svc"
  else
    echo "  (note: $svc not loaded — run deploy/cloudflare/launchagents/install.sh first)"
  fi
done

echo "== Smoke test =="
[ "$DRY_RUN" = 1 ] || sleep 4
run "$here/smoke_prod.sh"

echo "== Promotion to $tag complete =="
