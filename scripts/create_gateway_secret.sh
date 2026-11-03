#!/usr/bin/env bash
# Creates the Modal secret `model-in-modal-gateway`. Prints no secret values.
#
#   SESSION_SIGNING_KEY       random; generated into ./.env here if missing
#   UPSTASH_REDIS_REST_URL    from ./.env (you add it from the Upstash console)
#   UPSTASH_REDIS_REST_TOKEN  from ./.env (you add it from the Upstash console)
#   TURNSTILE_SECRET          from ./.env if set; otherwise Cloudflare's public
#                             always-pass TEST secret (fine until the public launch)
#
# Values reach Modal through a temporary 0600 JSON file, never the command line.
# Safe to re-run (e.g. after adding the real TURNSTILE_SECRET in Phase 5).
set -euo pipefail
cd "$(dirname "$0")/.."
MODAL=.venv/bin/modal
TEST_TURNSTILE_SECRET="1x0000000000000000000000000000000AA"  # public, documented by Cloudflare

touch .env
chmod 600 .env
# Appends below need .env to end with a newline, or the new line would be glued
# onto the last existing value.
if [ -s .env ] && [ -n "$(tail -c1 .env)" ]; then echo >> .env; fi

if ! grep -qE '^SESSION_SIGNING_KEY=.+' .env; then
  echo "SESSION_SIGNING_KEY=$(openssl rand -hex 32)" >> .env
  echo "Generated SESSION_SIGNING_KEY"
fi
for k in UPSTASH_REDIS_REST_URL UPSTASH_REDIS_REST_TOKEN; do
  if ! grep -qE "^$k=.+" .env; then
    echo "Missing $k in .env" >&2
    exit 1
  fi
done

tmpdir=$(mktemp -d)
trap 'rm -rf "$tmpdir"' EXIT
chmod 700 "$tmpdir"
umask 077
if grep -qE '^TURNSTILE_SECRET=.+' .env; then
  echo "TURNSTILE_SECRET: using the value in .env"
else
  echo "TURNSTILE_SECRET: none in .env, using Cloudflare's always-pass TEST secret"
fi
# Build the JSON with the standard library (modal's --from-dotenv needs python-dotenv).
TEST_TURNSTILE_SECRET="$TEST_TURNSTILE_SECRET" python3 - "$tmpdir/gateway.json" <<'PY'
import json, os, sys
wanted = ["SESSION_SIGNING_KEY", "TURNSTILE_SECRET", "UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN"]
env = {}
for line in open(".env"):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
values = {k: env[k] for k in wanted if env.get(k)}
values.setdefault("TURNSTILE_SECRET", os.environ["TEST_TURNSTILE_SECRET"])
with open(sys.argv[1], "w") as f:
    json.dump(values, f)
PY

"$MODAL" secret create --force model-in-modal-gateway --from-json "$tmpdir/gateway.json" > /dev/null
echo "Modal secret ready: model-in-modal-gateway (values not printed)"
