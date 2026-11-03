#!/usr/bin/env bash
# Creates the Modal secrets needed for Phase 1. Prints no secret values.
#
#   model-in-modal-vllm   VLLM_API_KEY                     (random, generated here)
#   model-in-modal-proxy  MODAL_PROXY_KEY, MODAL_PROXY_SECRET (new Modal proxy auth token)
#
# Values are also kept in ./.env (git-ignored, chmod 600) so local smoke tests
# can read them. Safe to re-run: existing values in .env are reused.
set -euo pipefail
cd "$(dirname "$0")/.."
MODAL=.venv/bin/modal

touch .env
chmod 600 .env

if ! grep -q '^VLLM_API_KEY=' .env; then
  echo "VLLM_API_KEY=$(openssl rand -hex 32)" >> .env
  echo "Generated VLLM_API_KEY"
fi

if ! grep -q '^MODAL_PROXY_KEY=' .env; then
  "$MODAL" workspace proxy-tokens create --name model-in-modal-gateway --json \
    | python3 -c '
import json, sys

def walk(o):
    if isinstance(o, dict):
        for v in o.values():
            yield from walk(v)
    elif isinstance(o, list):
        for v in o:
            yield from walk(v)
    elif isinstance(o, str):
        yield o

vals = list(walk(json.load(sys.stdin)))
key = next(v for v in vals if v.startswith("wk-"))
secret = next(v for v in vals if v.startswith("ws-"))
print(f"MODAL_PROXY_KEY={key}")
print(f"MODAL_PROXY_SECRET={secret}")
' >> .env
  echo "Created proxy auth token 'model-in-modal-gateway'"
fi

set -a
# shellcheck disable=SC1091
source .env
set +a

"$MODAL" secret create --force model-in-modal-vllm \
  VLLM_API_KEY="$VLLM_API_KEY" > /dev/null
"$MODAL" secret create --force model-in-modal-proxy \
  MODAL_PROXY_KEY="$MODAL_PROXY_KEY" MODAL_PROXY_SECRET="$MODAL_PROXY_SECRET" > /dev/null

echo "Modal secrets ready: model-in-modal-vllm, model-in-modal-proxy (values not printed)"
