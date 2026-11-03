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
GPU_TYPE = "L4"
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

# ---------------------------------------------------------------------------
# Prices — https://modal.com/pricing (checked 2026-11-02; matches `modal billing rates`)
# ---------------------------------------------------------------------------
PRICE_L4_PER_S = 0.000222
PRICE_CPU_CORE_PER_S = 0.0000131  # per physical core
PRICE_MEMORY_GIB_PER_S = 0.00000222

# ---------------------------------------------------------------------------
# Budget / spend meter
# ---------------------------------------------------------------------------
SOFT_CAP_USD = 27.0  # Modal workspace budget of $30 is the hard stop
METER_INTERVAL_S = 30
SPEND_CACHE_TTL_S = 30

# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------
SESSION_TTL_S = 24 * 60 * 60
TURNSTILE_VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"

# ---------------------------------------------------------------------------
# Rate limits (Upstash Redis)
# ---------------------------------------------------------------------------
SESSION_MSGS_PER_MIN = 10
SESSION_MSGS_PER_DAY = 50
SESSION_MAX_CONCURRENT_STREAMS = 1
IP_MSGS_PER_DAY = 150
GLOBAL_MAX_STREAMS = GPU_MAX_CONTAINERS * GPU_MAX_INPUTS  # 80
# Safety-net TTL so a crashed stream can't hold a slot forever. Must exceed the
# longest legitimate request (cold start + 512 tokens).
STREAM_SLOT_TTL_S = 10 * 60

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

# Upstream (gateway -> vLLM). Covers a GPU cold start.
UPSTREAM_CONNECT_TIMEOUT_S = 10
UPSTREAM_READ_TIMEOUT_S = 9 * 60


def allowed_origins() -> list[str]:
    return [f"https://{DUCKDNS_DOMAIN}", f"https://{GITHUB_USERNAME}.github.io"]


DEV_ORIGIN_REGEX = r"^http://(localhost|127\.0\.0\.1)(:\d+)?$"
