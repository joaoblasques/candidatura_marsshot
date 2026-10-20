#!/usr/bin/env bash
# Install (or refresh) the Nora agentics prod-side cron jobs on the always-on Mac Mini.
#
# These three loops run inside Nora's production environment (they touch the prod stores and
# can send to the principal), so they live in the Mini's crontab — not CI. The job is
# IDEMPOTENT: re-running it replaces the managed block, never duplicates it. It's also safe to
# install before opting features on — followups/voice_dream no-op until enabled in the Mini .env
# (NORA_VOICE_MEMORY=true, etc.); the keeper digest is read-only.
#
# Run once on the Mini:
#     cd ~/Nora_Bennett && git pull && bash scripts/install_mini_crons.sh
#
# Override the repo path or schedules via env vars if needed (see below).
set -euo pipefail

REPO="${NORA_REPO_ROOT:-$HOME/Nora_Bennett}"
# Resolve an ABSOLUTE uv path — cron runs with a minimal PATH that won't include ~/.local/bin,
# so a bare `uv` (or `/usr/bin/env uv`) would fail nightly. Prefer the installed location.
UV_BIN="$(command -v uv 2>/dev/null || true)"
if [ -z "$UV_BIN" ] && [ -x "$HOME/.local/bin/uv" ]; then UV_BIN="$HOME/.local/bin/uv"; fi
RUN="${UV_BIN:-uv} run"

BEGIN="# >>> nora-agentics (managed by scripts/install_mini_crons.sh) >>>"
END="# <<< nora-agentics <<<"

block="$(cat <<EOF
$BEGIN
# Nightly project-health digest (read-only) -> docs/agentics/digests/<date>.md
${KEEPER_CRON:-12 8 * * *}  cd $REPO && $RUN python -m scripts.agentics.keeper   >> \$HOME/nora-keeper.log 2>&1
# Nightly: remind the principal about un-approved proposals (self-send only; opt-in)
${FOLLOWUPS_CRON:-22 8 * * *}  cd $REPO && $RUN python -m app.followups          >> \$HOME/nora-followups.log 2>&1
# Nightly: consolidate per-person voice profiles (deterministic; opt-in)
${VOICE_CRON:-32 8 * * *}  cd $REPO && $RUN python -m app.voice_dream            >> \$HOME/nora-voice.log 2>&1
$END
EOF
)"

current="$(crontab -l 2>/dev/null || true)"

# Strip any previously-installed managed block, keeping everything else verbatim.
cleaned="$(printf '%s\n' "$current" | awk -v b="$BEGIN" -v e="$END" '
  $0==b {skip=1; next} $0==e {skip=0; next} !skip {print}')"

# Reinstall: existing crontab (minus old block) + the fresh managed block.
{
  printf '%s\n' "$cleaned" | sed '/^$/d'
  printf '%s\n' "$block"
} | crontab -

echo "Installed nora-agentics crons on $(hostname):"
crontab -l | sed -n "/nora-agentics/,/<<< nora-agentics/p"
