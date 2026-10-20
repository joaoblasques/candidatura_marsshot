#!/usr/bin/env bash
# Gates before anything is published. Fails closed: missing tool or names list => exit 2.
#   1 secrets in the mirror   2 client/person names in the mirror (hidden files included)
#   3 names anywhere else in the repo   4 names in commit authors/messages   5 mirror tests, no credentials
set -uo pipefail
DIR="${1:-nora}"
NAMES_FILE="${NAMES_FILE:-private/names.txt}"            # mirror: also bans the client name
NAMES_SITE_FILE="${NAMES_SITE_FILE:-private/names-site.txt}"  # rest of repo: client may be named
REPO_ROOT="${REPO_ROOT:-.}"

command -v gitleaks >/dev/null || { echo "gate preflight: gitleaks not found" >&2; exit 2; }
command -v rg >/dev/null || { echo "gate preflight: rg not found" >&2; exit 2; }
for f in "$NAMES_FILE" "$NAMES_SITE_FILE"; do
  [ -s "$f" ] || { echo "gate preflight: $f missing or empty" >&2; exit 2; }
done
[ -d "$DIR" ] || { echo "gate preflight: $DIR is not a directory" >&2; exit 2; }

echo "== gate 1: secrets"
gitleaks dir "$DIR" --no-banner --redact || exit 1

echo "== gate 2: names in the mirror (hidden files included)"
hits=$(rg -il --hidden --no-ignore -g '!.venv/' -g '!.git/' -f "$NAMES_FILE" -- "$DIR")
if [ -n "$hits" ]; then echo "$hits"; echo "names found in files above" >&2; exit 1; fi

echo "== gate 3: names in the rest of the repo"
prefix="${DIR%/}/"
site_abs="$(cd "$(dirname "$NAMES_SITE_FILE")" && pwd)/$(basename "$NAMES_SITE_FILE")"
# /dev/null keeps rg from scanning the cwd if the file list is empty
hits=$(cd "$REPO_ROOT" && git ls-files -z --cached --others --exclude-standard \
  | { grep -zv "^${prefix}" || true; } | xargs -0 rg -il -f "$site_abs" -- /dev/null 2>/dev/null)  # 2>: tracked-but-deleted paths
if [ -n "$hits" ]; then echo "$hits"; echo "names found in files above" >&2; exit 1; fi

echo "== gate 4: names in git history (authors and messages)"
# capture first, no pipe: rg -q closing a pipe early makes git log die of SIGPIPE, which pipefail reads as "no match"
history=$(git -C "$REPO_ROOT" log --format='%an <%ae>%n%B' 2>/dev/null)
if rg -iq -f "$NAMES_SITE_FILE" <<<"$history"; then
  echo "names found in git history (author or message); rewrite before pushing" >&2; exit 1
fi

if [ -z "${SKIP_PYTEST:-}" ]; then
  echo "== gate 5: mirror tests with no credentials"
  env -u ANTHROPIC_API_KEY -u OPENROUTER_API_KEY -u RESEND_API_KEY \
    $(env | sed -n 's/^\(NORA_[^=]*\)=.*/-u \1/p') \
    uv run --directory "$DIR" pytest -q || exit 1
fi
echo "all gates passed"
