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
| Expected vLLM startup with all caches warm | ~80 s (estimate) + GPU queue time |
| Scale to zero after last request | 2 min 32 s / 2 min 53 s |
| Auth: no proxy token / proxy token without vLLM key | 401 / 401 |

Modal returns `303 See Other` to any request still waiting after 150 s, so cold starts
longer than that are visible to the caller as redirects.
