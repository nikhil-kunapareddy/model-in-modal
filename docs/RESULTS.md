# Measured results, phase by phase

Raw results recorded while building model-in-modal (2026-11-02/03). The [README](../README.md)
summarizes them. Costs come from the spend meter, which reads ~14% high versus Modal's bill
(it adds a 30 s boot allowance per container).

## Phase 1 — vLLM alone (2026-11-03, vLLM 0.32.0, 1× L4)

| Metric | Value |
|---|---|
| Weights | `Qwen/Qwen3.5-9B` @ `c202236`, BF16 on disk (19.3 GB), FP8 online quantization |
| GPU memory for weights | 10.28 GiB |
| KV cache (FP8) | 7.48 GiB = 271,506 tokens (33× full 8,192-token requests) |
| Time to first token (warm, ~30-token prompt) | 0.42–0.84 s |
| Decode speed, single request | ~24 tok/s |
| Cold start #1 (empty caches, first image pull) | 894 s client-side; vLLM init 353 s |
| Cold start #2 (compile + Triton cache warm) | 540 s client-side; vLLM init 175 s |
| — of which waiting for L4 capacity | ~330 s ("waiting to be scheduled on a GPU_L4 worker") |
| — one-time FlashInfer JIT (now cached on Volume) | ~130 s |
| Cold start #3 (FlashInfer cached; compile cache invalidated by flag change) | vLLM init 161 s, of which compile 134 s; warmup 1.4 s |
| Expected vLLM startup with all caches warm | ~75 s + GPU queue time (queue is not billed) |
| Scale to zero after last request | 2 min 32 s / 2 min 53 s |
| Auth: no proxy token / proxy token without vLLM key | 401 / 401 |

Modal returns `303 See Other` to any request still waiting after 150 s, so cold starts
longer than that are visible to the caller as redirects.

## Phase 2 — spend meter (2026-11-03)

| Check | Result |
|---|---|
| Meter key appears | at container start, 26 s before vLLM launches |
| Update interval | every ~30.5 s while running |
| Final write on exit | yes, on SIGTERM at scale-down |
| Updates after scale-down | none (key frozen) |
| Meter vs Modal billing, one container | meter $0.1183 (357 s + 30 s boot allowance) vs Modal $0.1036 (~364 s billed) — meter reads ~14% high, the safe direction |
| GPU queue time billed? | no — Modal bills from container start |
| Daily compaction job on Modal | writes `{cycle}:closed`, leaves fresh keys alone |

## Phase 3 — gateway (2026-11-03)

| Check | Result |
|---|---|
| Unit + in-process tests | 151 pytest tests: tokens, Turnstile, rate limits (fake Redis), shaping, gateway endpoints, CORS, logging, meter |
| Rate limiter against real Upstash | INCR+EXPIRE in one MULTI/EXEC, every key has a TTL, slots and refunds behave as in tests |
| Deployed end-to-end (`scripts/e2e_gateway.py`) | 15/15 checks |
| L4 availability, 10:53–11:38 PST | no L4 scheduled for 35+ min ("waiting to be scheduled on a GPU_L4 worker"); queued calls cost nothing |
| GPU fallback (approved) | `gpu=["L4", "L40S"]`; meter reads `nvidia-smi` and bills the GPU it got (L4 $1.02/hr, L40S $2.17/hr) |
| Cold start through the gateway | ready 187 s after the session was created: ~64 s to get an L40S, 116 s vLLM init (incl. 46 s one-time L40S compile, now cached) |
| Visitor experience while cold | 18 `503 warming_up` replies (retry_after 10 s), answered without Redis or GPU calls |
| Warm time to first token (L40S) | 0.14–0.30 s at the gateway, 0.27–0.56 s at the client |
| Decode speed, single request (L40S) | ~62 tok/s (111 tokens in 1.78 s) |
| Second stream in the same session | 429, `retry_after` 3 |
| Per-minute limit | 429 with `retry_after` = seconds to the next minute, plus `Retry-After` header |
| Real client IP on Modal | ASGI peer address; Modal strips a client-sent `X-Forwarded-For` and passes `X-Real-IP` through unchanged (spoofable), so no header is trusted |
| Logs | session id, IP, prompt chars, output tokens, TTFT, latency, status; checked to contain no message text |
| Cost of the run | L40S 177 s = $0.125 (meter), gateway ~$0.009 |

## Phase 4 — webpage (2026-11-03)

`web/` is plain HTML/CSS/JS (no framework, no build step), light and dark mode, mobile-first.
`scripts/browser_check.py` drives real Safari through Safari's built-in `safaridriver`
(W3C WebDriver over plain HTTP, no extra packages) against the dev-mode gateway with
Cloudflare's Turnstile test keys.

| State | How it was forced | Result |
|---|---|---|
| Offline | app stopped | red banner next to the input + Retry, input disabled |
| Turnstile failure | always-fail test sitekey | "We couldn't verify your browser" + Try again, no session |
| Session | always-pass test sitekey | Turnstile → `/api/session` in 3–9 s, token in `sessionStorage` |
| Cold model | real cold start | bubble shows "Waking up the model (m:ss)…", auto-retries; first reply 66–84 s after sending (L40S) |
| Streaming | real | tokens render as they arrive (`fetch` + `ReadableStream`, not `EventSource`) |
| 401 | stored token corrupted, page reloaded | Turnstile re-run, request retried once, reply streamed |
| Stop | button during a reply | partial reply kept, marked "Stopped." |
| 429 per minute | 10+ quick messages | sticky countdown ("send again in 0:19"), ticks, clears; typed text restored |
| 503 busy | redeploy with `GLOBAL_MAX_STREAMS = 0` | "The model is busy right now" |
| 503 credits_exhausted | redeploy with `SOFT_CAP_USD = 0.5` | mid-chat and on load: banner with the reset date in local time, input disabled |

Bugs found by these runs and fixed: the gateway sent the final SSE event before freeing the
stream slot (an instant follow-up got a false "one at a time" 429; now released first, with a
regression test), and the status banner scrolled away in long chats (now inside the sticky
composer). Browser-test GPU cost: ~$0.25 (two L40S runs).

## Phase 5 — public deploy (2026-11-03)

| Check | Result |
|---|---|
| Public URL | **https://modelinmodal.duckdns.org** — GitHub Pages via `.github/workflows/pages.yml` (exact-pinned actions) |
| DNS | DuckDNS A record → 185.199.108.153; same answer from 1.1.1.1, 8.8.8.8, 9.9.9.9 |
| HTTPS | GitHub certificate approved ~20 min after DNS; Enforce HTTPS on; `http://` → 301 `https://` |
| Fallback origin | `nikhil-kunapareddy.github.io/model-in-modal` → 301 to the custom domain |
| Backend mode | production (no localhost CORS, no header logging); 8/8 API checks |
| vLLM endpoint without proxy token | 401 |
| Forced `credits_exhausted` (`SOFT_CAP_USD = 0.5`, live site) | banner with the reset date, input disabled, no session created so no GPU woke; normal cap restored after |
| Real visitor (phone) | Turnstile → session; GPU (L40S) started 10 s later; vLLM ready 76 s after the session; reply TTFT 162 ms at the gateway |
| Turnstile vs. automation | the real widget shows "Verify you are human" to WebDriver-driven Safari, as designed |
| Scale to zero | all containers gone ~2 min 45 s after the last message (120 s window + shutdown) |
| Cost of the live test | $0.16 (L40S, 240 s) |

## Phase 6 — load test (2026-11-03)

k6 v2.3.0 (`scripts/loadtest/chat.js`) ramps 1 → 10 → 40 → 80 streaming users over 4 min, holds 80
for 3 min, then ramps down; each user sends a prompt asking for a ~150-word answer, waits 1–3 s, repeats.
Requests carry the load-test header (skips per-session/per-IP limits only — budget check, global
cap and shaping still apply; disabled again after each run). Session tokens are signed locally, so
Turnstile stays on for real visitors. `monitor.py` sampled streams in flight and live containers.

**Run 1** found the bottleneck: one gateway container (0.25 core reserved) relaying one SSE message
per token sat flat at ~1.2 cores — a single Python event loop at full speed — while vLLM never had more
than 25 requests generating and its queue stayed empty. **Run 2** after three changes: reserve 1 core per
gateway, `@modal.concurrent(target_inputs=40)` so the autoscaler spreads streams over both allowed
gateway containers, and relay tokens in ≤50 ms batches (first token still sent at once).

| Metric | Run 1 (before) | **Run 2 (after)** |
|---|---|---|
| Concurrency reached (streams in flight) | 78 | 68 (same 80 users; replies finish faster) |
| Containers at peak | 1 gateway, 1 L40S | 2 gateways, 2 L40S |
| Streams completed in the 9-min run | 1,543 | **3,102** |
| Time to first token p50 / p95 | 0.52 s / 1.45 s | 0.93 s / 2.86 s |
| Tokens/s per user p50 (p95) | 12.5 (55) | **43 (49)** |
| Total response time p50 / p95 | 14.4 s / 26.8 s | **5.3 s / 8.5 s** |
| Aggregate output | 583 tok/s | **1,153 tok/s** (peak 1,580 tok/s per GPU) |
| Error rate | 0% (0 / 1,543) | **0% (0 / 3,102)** |
| GPU cost | $0.36 | $0.52 |

![Response time vs. concurrent streams, after the fix](loadtest.svg)

Before the fix: [loadtest-before.svg](loadtest-before.svg). Time to first token got slower in
run 2 and is bimodal (~0.25 s and ~0.9 s clusters); the likely cause is a gateway container placed in a
different region from the GPU and Upstash (Modal places unpinned containers anywhere). Containers now
log their region at startup to confirm; pinning the gateway's region is an open decision (it costs a
1.15–1.75× multiplier on the gateway's ~$0.05/hr).

### Table view of the graphs (per 5-stream bin)

Run 1 (before), 1,544 requests:

| Streams in flight | Requests | TTFT median / p95 (s) | Total median / p95 (s) | Tokens/s per user median / p5 |
|---|---|---|---|---|
| 1–5 | 47 | 0.23 / 0.28 | 3.5 / 5.1 | 60.4 / 58.0 |
| 6–10 | 63 | 0.24 / 0.37 | 3.6 / 5.5 | 57.9 / 55.1 |
| 11–15 | 71 | 0.26 / 0.32 | 3.8 / 5.7 | 55.0 / 50.6 |
| 16–20 | 101 | 0.31 / 0.38 | 4.3 / 6.7 | 50.7 / 43.2 |
| 21–25 | 76 | 0.35 / 0.51 | 4.5 / 6.8 | 49.0 / 36.8 |
| 26–30 | 70 | 0.38 / 0.51 | 5.5 / 9.0 | 38.5 / 31.2 |
| 31–35 | 70 | 0.42 / 1.28 | 6.7 / 12.0 | 32.0 / 24.7 |
| 36–40 | 54 | 0.44 / 0.60 | 8.2 / 14.3 | 25.1 / 18.2 |
| 41–45 | 48 | 0.48 / 0.64 | 10.4 / 16.9 | 21.1 / 17.4 |
| 46–50 | 43 | 0.48 / 0.61 | 10.4 / 19.8 | 17.7 / 14.5 |
| 51–55 | 56 | 0.54 / 2.09 | 14.3 / 20.3 | 15.5 / 13.1 |
| 56–60 | 57 | 0.52 / 2.08 | 14.0 / 24.0 | 13.6 / 11.9 |
| 61–65 | 102 | 0.62 / 2.55 | 17.5 / 28.3 | 11.3 / 10.8 |
| 66–70 | 355 | 0.60 / 2.71 | 17.9 / 28.7 | 11.1 / 10.2 |
| 71–75 | 306 | 0.60 / 0.82 | 18.0 / 28.0 | 11.1 / 10.0 |
| 76–80 | 25 | 0.62 / 0.76 | 16.2 / 19.9 | 11.1 / 10.7 |

Run 2 (after), 3,103 requests:

| Streams in flight | Requests | TTFT median / p95 (s) | Total median / p95 (s) | Tokens/s per user median / p5 |
|---|---|---|---|---|
| 1–5 | 32 | 0.92 / 1.09 | 4.5 / 6.3 | 56.7 / 54.6 |
| 6–10 | 51 | 0.92 / 1.20 | 4.4 / 6.3 | 55.8 / 53.7 |
| 11–15 | 77 | 0.92 / 1.01 | 4.6 / 6.7 | 53.4 / 50.1 |
| 16–20 | 99 | 0.94 / 1.01 | 4.8 / 6.7 | 50.8 / 47.2 |
| 21–25 | 103 | 0.95 / 1.01 | 4.9 / 6.6 | 49.1 / 45.7 |
| 26–30 | 105 | 0.98 / 1.38 | 4.9 / 7.3 | 46.7 / 43.0 |
| 31–35 | 106 | 0.99 / 1.27 | 5.1 / 7.8 | 45.5 / 42.8 |
| 36–40 | 119 | 0.89 / 1.36 | 5.2 / 8.1 | 43.8 / 40.8 |
| 41–45 | 202 | 0.70 / 1.56 | 4.9 / 7.7 | 44.2 / 39.8 |
| 46–50 | 459 | 0.61 / 1.74 | 4.9 / 7.6 | 47.1 / 41.3 |
| 51–55 | 717 | 0.61 / 2.29 | 4.8 / 7.7 | 47.1 / 41.5 |
| 56–60 | 642 | 0.75 / 2.90 | 5.1 / 7.9 | 47.2 / 41.7 |
| 61–65 | 315 | 1.01 / 3.74 | 5.2 / 9.6 | 46.8 / 40.9 |
| 66–70 | 72 | 3.07 / 4.43 | 7.4 / 10.9 | 43.4 / 40.4 |
| 71–75 | 4 | 3.46 / 4.51 | 8.7 / 10.0 | 41.5 / 40.5 |
