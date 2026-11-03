# Load test

k6 (v2.3.0, single binary: `brew install k6` or the release zip) ramps concurrent
streaming chat users against the deployed gateway and reports time to first token,
tokens/sec per stream, and error rate. `monitor.py` samples streams in flight and
live GPU containers at the same time.

## Safety

- The `X-Loadtest-Key` header skips **per-session and per-IP limits only**. The
  session token, the budget check (`SOFT_CAP_USD`), the global stream cap (80) and
  request shaping (`max_tokens` 512) always apply. Browsers can't send the header
  (it is not in the CORS allow-list).
- k6 can't pass Turnstile, so `make_tokens.py` signs ordinary session tokens locally
  with the gateway's signing key. Turnstile stays on for everyone else.
- Disable the bypass after a run: `scripts/create_loadtest_secret.sh --disable`, then redeploy.

## Run

```sh
scripts/create_loadtest_secret.sh              # once: enable the bypass (prints no values)
.venv/bin/modal deploy -m backend.app          # gateway picks up the secret
.venv/bin/python scripts/loadtest/make_tokens.py 80
.venv/bin/python scripts/loadtest/monitor.py --minutes 20 &      # optional sampler
k6 run --summary-export=scripts/loadtest/results/summary.json scripts/loadtest/chat.js
scripts/create_loadtest_secret.sh --disable && .venv/bin/modal deploy -m backend.app
```

`PEAK` (default 80) and `HOLD` (default 3m) are k6 env vars: `k6 run -e PEAK=40 ...`.
Results (tokens, CSV, summary) go to `results/`, which is git-ignored.
