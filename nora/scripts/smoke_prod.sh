#!/usr/bin/env bash
# Post-promotion smoke test for the Nora production instance.
# Automated checks: public health endpoint + LaunchAgents running. Exits non-zero
# on failure so promote.sh can gate on it. The real-email round-trip is manual.
set -uo pipefail

HOST="${NORA_PROD_URL:-https://app.example.com}"
fail=0

echo "== health: $HOST/health =="
if curl -fsS -m 10 "$HOST/health" 2>/dev/null | grep -q '"status":"ok"'; then
  echo "  PASS"
else
  echo "  FAIL: $HOST/health did not return status ok"
  fail=1
fi

echo "== LaunchAgents =="
uid=$(id -u)
for svc in com.nora.app com.nora.tunnel; do
  if launchctl print "gui/$uid/$svc" >/dev/null 2>&1; then
    echo "  PASS: $svc loaded"
  else
    echo "  FAIL: $svc not loaded"
    fail=1
  fi
done

echo
if [ "$fail" = 0 ]; then
  echo "SMOKE: PASS (automated checks)"
else
  echo "SMOKE: FAIL — see above"
fi

echo
echo "Manual step (can't be automated without sending real mail):"
echo "  Forward a thread to nora@nora-bennett.com with WIYOW on the first line;"
echo "  confirm Nora's reply lands in the CEO inbox."

exit $fail
