# model-in-modal

A public chat page backed by a self-hosted open-weight LLM (Qwen3.5-9B, FP8) served by
vLLM on Modal, rate-limited and capped to run on Modal's $30/month free credit.

> Work in progress. Full architecture, setup and cost-control docs land in Phase 7.

## Measured results

### Phase 1 — vLLM alone (2026-11-03, vLLM 0.32.0, 1× L4)

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

### Phase 2 — spend meter (2026-11-03)

| Check | Result |
|---|---|
| Meter key appears | at container start, 26 s before vLLM launches |
| Update interval | every ~30.5 s while running |
| Final write on exit | yes, on SIGTERM at scale-down |
| Updates after scale-down | none (key frozen) |
| Meter vs Modal billing, one container | meter $0.1183 (357 s + 30 s boot allowance) vs Modal $0.1036 (~364 s billed) — meter reads ~14% high, the safe direction |
| GPU queue time billed? | no — Modal bills from container start |
| Daily compaction job on Modal | writes `{cycle}:closed`, leaves fresh keys alone |

### Phase 3 — gateway (2026-11-03)

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
