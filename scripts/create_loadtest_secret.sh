#!/usr/bin/env bash
# Creates/updates the Modal secret `model-in-modal-loadtest`. Prints no secret values.
#
#   scripts/create_loadtest_secret.sh            enable: random LOADTEST_KEY (kept in ./.env)
#   scripts/create_loadtest_secret.sh --disable  disable: LOADTEST_KEY=off in Modal
#
# The key lets the load test skip per-session and per-IP limits only; the budget
# check and the global stream cap always apply. Gateway containers read secrets at
# start, so redeploy after changing it.
set -euo pipefail
cd "$(dirname "$0")/.."
MODAL=.venv/bin/modal

tmpdir=$(mktemp -d)
trap 'rm -rf "$tmpdir"' EXIT
chmod 700 "$tmpdir"
umask 077

if [ "${1:-}" = "--disable" ]; then
  echo '{"LOADTEST_KEY": "off"}' > "$tmpdir/lt.json"
  "$MODAL" secret create --force model-in-modal-loadtest --from-json "$tmpdir/lt.json" > /dev/null
  echo "Load-test bypass DISABLED in Modal (redeploy the app to apply)"
  exit 0
fi

touch .env
chmod 600 .env
if [ -s .env ] && [ -n "$(tail -c1 .env)" ]; then echo >> .env; fi
if ! grep -qE '^LOADTEST_KEY=.{32,}' .env; then
  echo "LOADTEST_KEY=$(openssl rand -hex 32)" >> .env
  echo "Generated LOADTEST_KEY"
fi
python3 - "$tmpdir/lt.json" <<'PY'
import json, sys
env = dict(l.strip().split("=", 1) for l in open(".env") if "=" in l and not l.startswith("#"))
json.dump({"LOADTEST_KEY": env["LOADTEST_KEY"]}, open(sys.argv[1], "w"))
PY
"$MODAL" secret create --force model-in-modal-loadtest --from-json "$tmpdir/lt.json" > /dev/null
echo "Load-test bypass ENABLED: Modal secret model-in-modal-loadtest ready (value not printed)"
