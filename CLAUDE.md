# model-in-modal — project instructions for Claude Code

You are building **model-in-modal**: a public chat webpage where anyone can talk to a self-hosted open-weight LLM (Qwen3.5-9B) served by vLLM on Modal. Usage is rate-limited per visitor and capped so the whole service runs entirely on Modal's $30/month free credit. When the credit for the billing cycle is used up, the service shows a friendly "out of credits until <reset date>" message and nobody can use it until the next cycle.

This is an experiment and a portfolio project. Favor simple, readable, well-tested code over cleverness, and keep the architecture exactly as described below unless the user approves a change.

---

## 0. Project inputs

Before starting Phase 1, make sure every value below is filled in. If any is blank, ask the user for it and write the answer into this file.

| Name | Value | Used for |
|---|---|---|
| `DUCKDNS_DOMAIN` | `modelinmodal.duckdns.org` | Public URL, CORS allowlist, Turnstile hostnames, GitHub Pages `CNAME` file |
| `GITHUB_USERNAME` | `nikhil-kunapareddy` | Repo and `<user>.github.io` fallback origin |
| `MODAL_WORKSPACE` | `nikhilkunapareddy` | Expected `*.modal.run` URLs |
| `BILLING_CYCLE_START_DAY` | `3` | Day of month Modal's credits reset (shown on Modal's Usage & Billing page) |
| `LOCAL_OS` | `macOS 27 (zsh)` | Shell commands in instructions |

---

## 1. Architecture (fixed decisions)

```
Browser (static page on GitHub Pages at DUCKDNS_DOMAIN)
  │  Turnstile challenge → session token (Authorization: Bearer)
  ▼
Gateway — FastAPI on a CPU-only Modal function (public *.modal.run URL)
  │  CORS allowlist · session verify · per-session + per-IP limits (Upstash Redis)
  │  global concurrency cap · budget check (spend meter) · request shaping
  │  adds Modal-Key / Modal-Secret proxy headers + vLLM API key
  ▼
Modal proxy + autoscaler (requires_proxy_auth=True, max 2 GPU containers)
  ▼
vLLM OpenAI-compatible server on 1× L4 per container (Qwen3.5-9B, FP8)
  │  writes its running cost to the spend meter (Modal Dict) every 30 s
  ▼
Modal Volume with model weights (+ a second Volume for vLLM compile cache)
```

Outside Modal: the static webpage (GitHub Pages), Cloudflare Turnstile, Upstash Redis. These stay up even if Modal stops workloads, so the page can show an offline message.

Hard backstop (set manually by the user, not in code): Modal workspace budget of **$30**, so usage can never exceed the free credit.

Key decisions — do not change without asking:
- Model: **Qwen3.5-9B**, FP8 weights, thinking mode **off** for every request.
- GPU: **L4**, `max_containers=2`, `min_containers=0` (scale to zero), short `scaledown_window` (~120 s).
- Auth between gateway and vLLM: Modal proxy auth token **and** vLLM `--api-key`.
- No cookies. The browser holds a signed session token and sends it as a bearer header (page and API are on different domains, so cookies would be third-party).
- Metering is by **container run time**, not tokens. Idle and cold-start time are billed by Modal.
- Soft cap in the app: **$27**. Modal's $30 budget is the hard stop.

---

## 2. Repository layout

The folder you are running in, `model-in-modal/`, is the repository root, and this `CLAUDE.md` is already in it. Create everything else directly inside this folder. Do not create a nested `model-in-modal/` subfolder, and do not move or overwrite this file (edit it only to fill in section 0). If the folder isn't a git repository yet, run `git init` here during Phase 0.

```
model-in-modal/
├── CLAUDE.md
├── README.md                  # architecture, setup, measured results
├── .gitignore                 # includes .env, __pycache__, .venv
├── .env.example               # names only, never real values
├── backend/
│   ├── app.py                 # Modal App "model-in-modal"; imports and registers everything below
│   ├── config.py              # all constants: prices, limits, names, model id, cycle day
│   ├── weights.py             # one-off function: download weights into the Volume
│   ├── vllm_server.py         # GPU class: spend meter + vLLM web server
│   ├── meter.py               # spend meter write/read helpers, billing-cycle id
│   ├── gateway.py             # FastAPI app (asgi_app on CPU)
│   ├── sessions.py            # sign/verify session tokens, Turnstile verify
│   ├── ratelimit.py           # Upstash Redis limits
│   └── shaping.py             # build the upstream request from the browser request
├── web/
│   ├── index.html
│   ├── app.js
│   ├── styles.css
│   ├── config.js              # API base URL, Turnstile site key (public values only)
│   └── CNAME                  # contains DUCKDNS_DOMAIN
├── scripts/
│   └── loadtest/              # k6 or Locust script + README
├── tests/                     # pytest: sessions, ratelimit logic, meter, shaping
└── .github/workflows/pages.yml  # deploy web/ to GitHub Pages
```

---

## 3. Component specifications

### 3.1 Weights (`weights.py`)
- Look up the exact Hugging Face repo id for Qwen3.5-9B and check whether an official FP8 checkpoint exists. Do not guess repo ids; confirm them.
- If an official FP8 checkpoint exists, use it. Otherwise download the BF16 checkpoint and have vLLM quantize at load time with `--quantization fp8` (the L4 supports FP8).
- Download into Volume `model-in-modal-weights`. Create Volume `model-in-modal-vllm-cache` for vLLM's compile/CUDA-graph cache.
- This is a CPU function, run once with `modal run`.

### 3.2 vLLM server (`vllm_server.py`)
- `gpu="L4"`, `cpu=2`, `memory=16384`, `max_containers=2`, `min_containers=0`, `scaledown_window=120`.
- `@modal.concurrent(max_inputs=40)` so overflow queues at Modal and triggers a second container.
- `@modal.web_server(port=8000, requires_proxy_auth=True)` with a startup timeout long enough for model load (several minutes).
- Pin a vLLM version that supports Qwen3.5. Verify every flag with `vllm serve --help` inside the image before relying on it. Target settings:
  - `--kv-cache-dtype fp8`
  - `--max-model-len 8192`
  - `--max-num-seqs 64`
  - `--gpu-memory-utilization 0.92`
  - image/video inputs disabled (text only)
  - `--api-key` from the `model-in-modal-vllm` secret
  - `--served-model-name qwen`
- Start the spend meter (3.3) at the very beginning of container startup, before launching vLLM.
- Confirm against current Modal docs how to combine `@app.cls` lifecycle hooks with `@modal.web_server`. If that pattern isn't supported, start the meter thread inside the web-server function.

### 3.3 Spend meter (`meter.py`)
- Prices live in `config.py` with a comment citing modal.com/pricing:
  - L4: `$0.000222/s`
  - CPU: `$0.0000131` per physical core per second
  - Memory: `$0.00000222` per GiB per second
- `RATE` is computed from the container's actual cpu/memory config, not hard-coded.
- Store spend in Modal Dict `model-in-modal-spend`, key `"{cycle_id}:{uuid4}"`, one key per container. Every 30 s, overwrite the key with the container's cumulative cost (`elapsed_seconds * RATE`). Do one final write on container exit.
- `cycle_id` is the start date of the current billing cycle, computed from `BILLING_CYCLE_START_DAY`. Handle months shorter than the start day.
- Meter the gateway's CPU containers the same way, with their own rate.
- `total_spend(cycle_id)` sums all keys for the cycle. The gateway caches it for 30 s.
- `SOFT_CAP_USD = 27`. At or above the cap, the service is unavailable until the next cycle.

### 3.4 Gateway (`gateway.py`, `sessions.py`, `ratelimit.py`, `shaping.py`)
- FastAPI served with `@modal.asgi_app()` on a small CPU container, high input concurrency, a small `max_containers`, scale to zero.
- Stream from vLLM with async `httpx` and pass the stream through to the browser as Server-Sent Events.
- **CORS:** allow only `https://DUCKDNS_DOMAIN` and `https://GITHUB_USERNAME.github.io` (plus `http://localhost:*` in dev mode only).
  - Methods: GET, POST.
  - Headers: `Authorization`, `Content-Type`.
  - No credentials.

**Endpoints**
- `GET /api/status` → `{available, reason, resets_at}`. Never expose secrets or exact spend.
- `POST /api/session` with `{turnstile_token}`:
  - Verify the token with Cloudflare's `siteverify` endpoint.
  - On success, return `{token, expires_at}`. The token is an HMAC-SHA256-signed string carrying a random session id and an expiry 24 h out.
- `POST /api/chat` with `{messages}`, sent with `Authorization: Bearer <token>`. Run these checks in order:
  1. Verify the token signature and expiry, else 401.
  2. Check the budget, else 503 `credits_exhausted` with `resets_at`.
  3. Check the per-session and per-IP limits, else 429 with `retry_after`.
  4. Check the global concurrency cap, else 503 `busy`.
  5. Shape the request.
  6. Forward and stream.

**Rate limits** (constants in `config.py`), using Upstash Redis with atomic `INCR` + `EXPIRE`:
- Per session: 10 messages/min, 50/day, and 1 response streaming at a time (counter with a TTL safety net so a crashed stream can't lock a session forever).
- Per IP: 150/day.
- Global concurrent streams: 80 (2 containers × 40).
- Find out which header carries the real client IP on Modal. Log the headers in dev, confirm with the user, and don't trust headers a client could spoof.

**Request shaping** (`shaping.py`) — never forward the browser's body unchanged:
- Use a fixed system prompt set on the server.
- Keep only the last N turns.
- Cap input length.
- Set `max_tokens=512`.
- Set `chat_template_kwargs={"enable_thinking": false}`.
- Fix temperature and top_p to sensible defaults.
- Use model name `qwen`.

**Upstream headers:** `Modal-Key`, `Modal-Secret` (proxy token) and `Authorization: Bearer <vLLM api key>`.

**Errors:** always JSON `{error, message, retry_after?, resets_at?}`.

**Logging:** session id, IP, prompt character count, output token count, latency, status. Never log message content or any secret.

### 3.5 Webpage (`web/`)
- Plain HTML/CSS/JS, no framework, no build step. Works on mobile; light and dark mode.
- On load:
  1. Call `/api/status`. If unavailable, show a banner with the reset date and disable input.
  2. Render the Turnstile widget and exchange its token at `/api/session`.
  3. Keep the session token in `sessionStorage`.
- Chat: `fetch()` POST and read `response.body` as a stream, parsing SSE and rendering tokens as they arrive. Do not use `EventSource`, because it can't send the Authorization header.
- Handle responses:
  - 401 → re-run Turnstile and retry once.
  - 429 → show a countdown.
  - 503 `busy` → "busy, try again shortly."
  - 503 `credits_exhausted` → banner with reset date.
  - Network error → "service offline."
- Show a short usage notice: experiment, rate-limited, don't share personal information.
- `config.js` holds only public values: the API base URL and the Turnstile site key.
- `web/CNAME` contains `DUCKDNS_DOMAIN`. Deploy with `.github/workflows/pages.yml`.

---

## 4. Secrets and manual steps

The user creates every account, token and secret themselves. You give exact instructions and commands with placeholder values. Never ask the user to paste a secret into the chat, and never print, log, or commit secret values.

Modal secrets the code expects (names only; document them in `.env.example` and the README):
- `model-in-modal-vllm`: `VLLM_API_KEY`
- `model-in-modal-proxy`: `MODAL_PROXY_KEY`, `MODAL_PROXY_SECRET` (from a Modal proxy auth token)
- `model-in-modal-gateway`: `SESSION_SIGNING_KEY`, `TURNSTILE_SECRET`, `UPSTASH_REDIS_REST_URL`, `UPSTASH_REDIS_REST_TOKEN`
- `huggingface` (only if the model repo requires it): `HF_TOKEN`

Manual steps to walk the user through, at the phase where each is needed:
1. **Modal:** add a card to unlock the $30/month credit. Set the workspace budget to **$30** on the Usage & Billing page. Create a proxy auth token.
2. **Upstash:** create a free Redis database and copy the REST URL and token into the Modal secret.
3. **Cloudflare Turnstile:** create a widget with hostnames `DUCKDNS_DOMAIN` and `localhost`. For local tests, use Cloudflare's documented test keys.
4. **GitHub Pages:** in the repo's Pages settings, add `DUCKDNS_DOMAIN` as the custom domain *before* changing DNS. After DNS resolves, enable "Enforce HTTPS."
5. **DuckDNS:** set the domain's IP to `185.199.108.153` (GitHub Pages). Warn the user never to run the DuckDNS IP auto-updater for this domain, since it would overwrite this IP. If GitHub can't issue HTTPS for the domain within a day, fall back to `GITHUB_USERNAME.github.io`.

---

## 5. Phases

Work one phase at a time. At the end of each phase, summarize what was built, how it was verified, and any open questions, then wait for the user's go-ahead.

0. **Setup check.**
   - Confirm every input in section 0 is filled.
   - Check local tooling: Python 3.11+, `modal` CLI logged in, `git`, `gh`.
   - Confirm the Hugging Face repo id and a vLLM version that supports Qwen3.5.
   - Propose `config.py` values.
1. **Weights and vLLM alone.**
   - Download weights.
   - Deploy the vLLM server.
   - Smoke-test with `curl` using the proxy token and vLLM key.
   - Record cold-start time and single-request tokens/sec.
2. **Spend meter.**
   - Implement and unit-test cycle computation and summing.
   - Verify keys appear in the Dict while a container runs and stop updating after it scales down.
3. **Gateway.**
   - Implement sessions, limits, budget check, shaping, streaming, and errors.
   - Write pytest tests for token sign/verify/expiry, limit logic (Redis mocked), cycle id, and shaping.
   - Test end to end against the deployed vLLM server.
4. **Webpage.**
   - Build it and test locally against the gateway in dev mode with Turnstile test keys.
   - Check every error state by forcing it.
5. **Public deploy.**
   - Set up the Pages workflow, `CNAME`, and DuckDNS.
   - Run end-to-end tests from the public URL, including a forced `credits_exhausted` (temporarily lower `SOFT_CAP_USD`).
6. **Load test.**
   - Write a k6 or Locust script that ramps concurrent streaming users.
   - Add a load-test bypass for per-session limits, enabled only by a separate secret header. It must never bypass the budget check.
   - Before running anything, estimate the cost and get approval.
   - Report concurrency reached, time to first token p50/p95, tokens/sec per user, and error rate.
7. **README.**
   - Architecture diagram (Mermaid), setup steps, cost controls, and measured results from Phase 6.

---

## 6. Working rules

- **Ask before spending credits.** Before any `modal deploy` or `modal run` that starts a GPU, and before any load test, state the expected GPU time and cost and wait for approval. Stop GPU apps when a test is finished.
- Never set `min_containers` above 0 or `max_containers` above 2 without asking.
- Verify APIs against current documentation (Modal, vLLM, Upstash, Turnstile, GitHub Pages) instead of relying on memory, and pin package versions. If docs contradict this file, stop and tell the user.
- Keep dependencies minimal: FastAPI, httpx, the Upstash Redis client, and pytest. No other frameworks or databases.
- All tunable numbers live in `config.py`.
- Commit at the end of each phase with a clear message. Never commit `.env` or secrets.
- When unsure about a requirement, ask rather than assume.

---

## 7. Definition of done

- A visitor at `https://DUCKDNS_DOMAIN` passes Turnstile, chats, and sees tokens stream in.
- Per-session and per-IP limits return 429 with a visible countdown.
- When the spend meter reaches the soft cap, every visitor sees the out-of-credits banner with the correct reset date, and no requests reach the GPU.
- The vLLM endpoint rejects any request without the proxy token.
- With no traffic, GPU containers scale to zero within about 2 minutes.
- Tests pass, and the README documents setup and measured load-test results.
