"""GPU class: vLLM OpenAI-compatible server for Qwen3.5-9B on one L4.

The endpoint requires Modal proxy auth (Modal-Key / Modal-Secret headers) and
the vLLM API key (Authorization: Bearer ...). vLLM reads the key from the
VLLM_API_KEY env var injected by the `model-in-modal-vllm` secret; this is the
same check as `--api-key`, but keeps the key out of the process list and out of
vLLM's startup log of non-default CLI args.
"""

import json
import subprocess

import modal

from backend import config
from backend.weights import vllm_cache_volume, weights_volume

app = modal.App(f"{config.APP_NAME}-vllm")

vllm_image = (
    modal.Image.from_registry(config.CUDA_BASE_IMAGE, add_python=config.PYTHON_VERSION)
    .entrypoint([])
    .uv_pip_install(f"vllm=={config.VLLM_VERSION}")
    .env(
        {
            "HF_HUB_OFFLINE": "1",  # weights come from the Volume, never the network
            "VLLM_CACHE_ROOT": config.VLLM_CACHE_MOUNT,
            "TRITON_CACHE_DIR": f"{config.VLLM_CACHE_MOUNT}/triton",
            # FlashInfer JIT-builds its sampling module on first use (~2 min on L4);
            # keep the build on the cache Volume so later cold starts skip it.
            "FLASHINFER_WORKSPACE_BASE": f"{config.VLLM_CACHE_MOUNT}/flashinfer",
        }
    )
)


def build_vllm_command() -> list[str]:
    """The exact `vllm serve` command line. Every flag is checked by check_vllm_flags."""
    return [
        "vllm",
        "serve",
        config.MODEL_DIR,
        "--served-model-name", config.SERVED_MODEL_NAME,
        "--host", "0.0.0.0",
        "--port", str(config.VLLM_PORT),
        "--quantization", "fp8",  # no official FP8 checkpoint: quantize BF16 at load
        "--kv-cache-dtype", "fp8",
        "--max-model-len", str(config.MAX_MODEL_LEN),
        "--max-num-seqs", str(config.MAX_NUM_SEQS),
        "--gpu-memory-utilization", str(config.GPU_MEMORY_UTILIZATION),
        "--language-model-only",  # text only: skip the vision encoder entirely
        "--default-chat-template-kwargs", json.dumps({"enable_thinking": False}),
        "--uvicorn-log-level", "warning",
    ]


@app.function(image=vllm_image, gpu=config.GPU_TYPE, cpu=1.0, memory=4096, timeout=5 * 60)
def check_vllm_flags() -> dict:
    """Every flag we pass exists in this vLLM build (~1 GPU-minute, ~$0.02).

    Needs a GPU: vLLM 0.32 can't build its arg parser without inferring a device.
    """
    import importlib.metadata
    import pathlib
    import re

    import vllm

    proc = subprocess.run(
        ["vllm", "serve", "--help=all"], capture_output=True, text=True, check=False
    )
    help_text = proc.stdout
    flags = [a for a in build_vllm_command() if a.startswith("--")]
    missing = [f for f in flags if not re.search(rf"(^|\s){re.escape(f)}[\s,=]", help_text)]

    # Confirm the VLLM_API_KEY env var is honored by the OpenAI server.
    pkg = pathlib.Path(vllm.__file__).parent
    api_key_env_uses = sorted(
        str(p.relative_to(pkg))
        for p in pkg.rglob("*.py")
        if "envs.VLLM_API_KEY" in p.read_text(errors="ignore")
    )
    request_logging_default = re.search(
        r"--enable-log-requests.*?(\n\s*\n|\Z)", help_text, re.S
    )

    return {
        "vllm_version": importlib.metadata.version("vllm"),
        "torch_version": importlib.metadata.version("torch"),
        "flags_checked": flags,
        "missing_flags": missing,
        "api_key_env_uses": api_key_env_uses,
        "enable_log_requests_help": request_logging_default.group(0)[:300]
        if request_logging_default
        else None,
        "help_chars": len(help_text),
        "help_returncode": proc.returncode,
        "help_stderr_tail": proc.stderr[-1500:],
    }


@app.cls(
    image=vllm_image,
    gpu=f"{config.GPU_TYPE}:{config.GPU_COUNT}",
    cpu=config.GPU_CPU_CORES,
    memory=config.GPU_MEMORY_MIB,
    min_containers=config.GPU_MIN_CONTAINERS,
    max_containers=config.GPU_MAX_CONTAINERS,
    scaledown_window=config.GPU_SCALEDOWN_WINDOW_S,
    timeout=config.VLLM_STARTUP_TIMEOUT_S,
    secrets=[modal.Secret.from_name(config.SECRET_VLLM, required_keys=["VLLM_API_KEY"])],
    volumes={
        config.WEIGHTS_MOUNT: weights_volume.read_only(),
        config.VLLM_CACHE_MOUNT: vllm_cache_volume,
    },
)
@modal.concurrent(max_inputs=config.GPU_MAX_INPUTS)
class VLLMServer:
    @modal.web_server(
        port=config.VLLM_PORT,
        startup_timeout=config.VLLM_STARTUP_TIMEOUT_S,
        requires_proxy_auth=True,
        label=f"{config.APP_NAME}-vllm",
    )
    def serve(self):
        self.process = subprocess.Popen(build_vllm_command())

    @modal.exit()
    def stop(self):
        proc = getattr(self, "process", None)
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                proc.kill()
