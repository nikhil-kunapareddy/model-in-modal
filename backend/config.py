"""All tunable constants for model-in-modal.

Every number the system uses lives here. Nothing in this file is secret.
"""

# ---------------------------------------------------------------------------
# Project inputs (CLAUDE.md section 0)
# ---------------------------------------------------------------------------
DUCKDNS_DOMAIN = "modelinmodal.duckdns.org"
GITHUB_USERNAME = "nikhil-kunapareddy"
MODAL_WORKSPACE = "nikhilkunapareddy"
BILLING_CYCLE_START_DAY = 3  # credits reset on the 3rd

# ---------------------------------------------------------------------------
# Modal object names
# ---------------------------------------------------------------------------
APP_NAME = "model-in-modal"
WEIGHTS_VOLUME = "model-in-modal-weights"
VLLM_CACHE_VOLUME = "model-in-modal-vllm-cache"
SPEND_DICT = "model-in-modal-spend"
SECRET_VLLM = "model-in-modal-vllm"  # VLLM_API_KEY
SECRET_PROXY = "model-in-modal-proxy"  # MODAL_PROXY_KEY, MODAL_PROXY_SECRET
SECRET_GATEWAY = "model-in-modal-gateway"  # SESSION_SIGNING_KEY, TURNSTILE_SECRET, UPSTASH_*

# ---------------------------------------------------------------------------
# Model and serving image
# ---------------------------------------------------------------------------
# Confirmed via the Hugging Face API on 2026-11-02: Qwen/Qwen3.5-9B exists, is
# Apache-2.0, not gated, BF16 (~19.3 GB). Qwen publishes no official FP8
# checkpoint for the 9B size, so vLLM quantizes to FP8 at load time.
MODEL_REPO = "Qwen/Qwen3.5-9B"
MODEL_REVISION = "c202236235762e1c871ad0ccb60c8ee5ba337b9a"  # pinned commit
SERVED_MODEL_NAME = "qwen"
VLLM_VERSION = "0.32.0"  # Qwen3.5 supported since 0.17.0; 0.32.0 released 2026-10-18
# vLLM 0.32.0 pins torch 2.13.0 (CUDA 13.0). Modal hosts ship CUDA driver 13.3.
CUDA_BASE_IMAGE = "nvidia/cuda:13.0.3-devel-ubuntu24.04"
PYTHON_VERSION = "3.12"
HF_HUB_VERSION = "2.2.0"  # for the one-off weights download

WEIGHTS_MOUNT = "/weights"
MODEL_DIR = f"{WEIGHTS_MOUNT}/{MODEL_REPO.split('/')[-1]}"
VLLM_CACHE_MOUNT = "/root/.cache/vllm"

# ---------------------------------------------------------------------------
# GPU container (vLLM)
# ---------------------------------------------------------------------------
# Preference order. Modal tries L4 first and uses an L40S only when no L4 can be
# scheduled (approved 2026-11-03 after a 35+ min L4 capacity gap). Both are Ada
# GPUs with native FP8, so the vLLM flags are identical.
GPU_TYPES = ("L4", "L40S")
GPU_COUNT = 1
GPU_CPU_CORES = 2.0  # Modal bills per physical core
GPU_MEMORY_MIB = 16384
GPU_MIN_CONTAINERS = 0  # never raise without asking
GPU_MAX_CONTAINERS = 2  # never raise without asking
GPU_SCALEDOWN_WINDOW_S = 120
GPU_MAX_INPUTS = 40  # per container; overflow queues and triggers container #2
VLLM_PORT = 8000
VLLM_STARTUP_TIMEOUT_S = 15 * 60  # weight load + FP8 quantize + compile

MAX_MODEL_LEN = 8192
MAX_NUM_SEQS = 64
GPU_MEMORY_UTILIZATION = 0.92

# ---------------------------------------------------------------------------
# Gateway container (CPU only)
# ---------------------------------------------------------------------------
GATEWAY_CPU_CORES = 0.25
GATEWAY_MEMORY_MIB = 512
GATEWAY_MIN_CONTAINERS = 0
GATEWAY_MAX_CONTAINERS = 2
GATEWAY_MAX_INPUTS = 200
GATEWAY_SCALEDOWN_WINDOW_S = 120
GATEWAY_REQUEST_TIMEOUT_S = 6 * 60  # per request; > warm wait + longest stream
GATEWAY_LABEL = f"{APP_NAME}-gateway"
GATEWAY_BASE_URL = f"https://{MODAL_WORKSPACE}--{GATEWAY_LABEL}.us-east.modal.run"  # from `modal deploy`
# Gateway image pins (checked on PyPI 2026-11-03; upstash-redis 1.9.0 was one day
# old, so the previous release is pinned).
FASTAPI_VERSION = "0.162.0"
HTTPX_VERSION = "0.28.1"
UPSTASH_REDIS_VERSION = "1.8.0"

# ---------------------------------------------------------------------------
# Prices — https://modal.com/pricing (checked 2026-11-02; matches `modal billing rates`)
# ---------------------------------------------------------------------------
PRICE_L4_PER_S = 0.000222
PRICE_L40S_PER_S = 0.000542
GPU_PRICES_PER_S = {"L4": PRICE_L4_PER_S, "L40S": PRICE_L40S_PER_S}
PRICE_CPU_CORE_PER_S = 0.0000131  # per physical core
PRICE_MEMORY_GIB_PER_S = 0.00000222

# ---------------------------------------------------------------------------
# Budget / spend meter
# ---------------------------------------------------------------------------
SOFT_CAP_USD = 27.0  # Modal's budget / spend limit is the hard stop
METER_INTERVAL_S = 30
SPEND_CACHE_TTL_S = 30
# Modal bills from container boot; the meter starts in @modal.enter. Count a fixed
# overhead per container for boot + shutdown time the meter can't see.
METER_STARTUP_OVERHEAD_S = 30
# Volumes are billed even with zero containers ($0.09/GiB/month, `modal billing
# rates`). Weights are 18.0 GiB; caches ~1-2 GiB. Counted once per cycle.
VOLUME_STORAGE_GIB = 20
PRICE_VOLUME_GIB_MONTH = 0.09
# Modal Dict entries expire after 7 days without reads/writes. A daily job folds
# finished containers into one per-cycle key and rewrites it.
METER_STALE_AFTER_S = 15 * 60  # no write for this long => container is gone
COMPACT_SCHEDULE_CRON = "17 4 * * *"  # daily, 04:17 UTC

# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------
SESSION_TTL_S = 24 * 60 * 60
TURNSTILE_VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
TURNSTILE_TIMEOUT_S = 10
MAX_TURNSTILE_TOKEN_CHARS = 2048  # Cloudflare's documented maximum
# Which request header carries the visitor's real IP on Modal. None means use the
# ASGI peer address (request.client.host). Phase 3 check (2026-11-03): Modal sets
# the peer to the real client IP, strips a client-sent X-Forwarded-For, and passes
# a client-sent X-Real-IP through unchanged (spoofable). So: no header.
CLIENT_IP_HEADER: str | None = None

# ---------------------------------------------------------------------------
# Rate limits (Upstash Redis)
# ---------------------------------------------------------------------------
SESSION_MSGS_PER_MIN = 10
SESSION_MSGS_PER_DAY = 50
SESSION_MAX_CONCURRENT_STREAMS = 1
IP_MSGS_PER_DAY = 150
GLOBAL_MAX_STREAMS = GPU_MAX_CONTAINERS * GPU_MAX_INPUTS  # 80
REDIS_TIMEOUT_S = 5  # per Upstash call; on failure the gateway fails closed (503)
# Retry hints sent with 429 / 503 responses.
STREAM_IN_PROGRESS_RETRY_AFTER_S = 3
BUSY_RETRY_AFTER_S = 5
WARMING_RETRY_AFTER_S = 10
UNAVAILABLE_RETRY_AFTER_S = 30

# ---------------------------------------------------------------------------
# Request shaping
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = (
    "You are a helpful, concise assistant running on a small self-hosted "
    "open-weight model as part of a public experiment. Answer clearly. "
    "If you are not sure, say so."
)
MAX_TURNS = 10  # most recent user/assistant messages kept
MAX_MESSAGE_CHARS = 4000
MAX_TOTAL_INPUT_CHARS = 12000
MAX_TOKENS = 512
# Qwen3.5 model card, recommended sampling for non-thinking mode.
TEMPERATURE = 0.7
TOP_P = 0.8
TOP_K = 20
PRESENCE_PENALTY = 1.5

MAX_BODY_BYTES = 256 * 1024  # request body cap for /api/session and /api/chat
MAX_BODY_MESSAGES = 200  # reject absurd histories before trimming to MAX_TURNS

# ---------------------------------------------------------------------------
# Upstream (gateway -> vLLM)
# ---------------------------------------------------------------------------
VLLM_LABEL = f"{APP_NAME}-vllm"
VLLM_BASE_URL = f"https://{MODAL_WORKSPACE}--{VLLM_LABEL}.us-east.modal.run"  # from `modal deploy`
UPSTREAM_CONNECT_TIMEOUT_S = 10
# Modal holds a request while a GPU container boots (5-15 min incl. GPU queue) and
# answers 303 after 150 s. If vLLM hasn't sent response headers within this many
# seconds, the gateway treats the model as cold: 503 warming_up, wake it, refund.
UPSTREAM_WARM_WAIT_S = 20
UPSTREAM_READ_TIMEOUT_S = 60  # max silence between streamed chunks
STREAM_MAX_DURATION_S = 5 * 60  # hard cap per response
# Safety-net TTL so a crashed stream can't hold a stream slot (or lock a session)
# forever. Must exceed the longest legitimate request.
STREAM_SLOT_TTL_S = UPSTREAM_WARM_WAIT_S + STREAM_MAX_DURATION_S + 40  # 6 min
# Wake-up ping after a new session: GET /health, re-sent until vLLM answers 200.
WAKE_PING_TIMEOUT_S = 170  # > Modal's 150 s redirect: let Modal end the wait, then follow it
WAKE_RETRY_PAUSE_S = 10  # after an unexpected error
WAKE_MAX_S = 25 * 60  # worst seen: 14.5 min GPU queue + ~3 min vLLM startup
# A GPU container stays up GPU_SCALEDOWN_WINDOW_S after its last request; the
# gateway assumes "warm" for a bit less than that.
WARM_ASSUME_S = GPU_SCALEDOWN_WINDOW_S - 20


def allowed_origins() -> list[str]:
    return [f"https://{DUCKDNS_DOMAIN}", f"https://{GITHUB_USERNAME}.github.io"]


DEV_ORIGIN_REGEX = r"^http://(localhost|127\.0\.0\.1)(:\d+)?$"
