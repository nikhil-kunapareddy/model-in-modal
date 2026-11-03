"""One-off CPU function: download the model weights into a Modal Volume.

Run once (CPU only, no GPU):
    .venv/bin/modal run -m backend.app::download_weights
"""

import modal

from backend import config

app = modal.App(f"{config.APP_NAME}-weights")

weights_volume = modal.Volume.from_name(config.WEIGHTS_VOLUME, create_if_missing=True)
vllm_cache_volume = modal.Volume.from_name(config.VLLM_CACHE_VOLUME, create_if_missing=True)

download_image = (
    modal.Image.debian_slim(python_version=config.PYTHON_VERSION)
    .uv_pip_install(f"huggingface_hub[hf_xet]=={config.HF_HUB_VERSION}")
    .env({"HF_XET_HIGH_PERFORMANCE": "1"})
)


@app.function(
    image=download_image,
    volumes={config.WEIGHTS_MOUNT: weights_volume},
    cpu=2.0,
    memory=4096,
    timeout=60 * 60,
)
def download_weights() -> dict:
    import os
    import time

    from huggingface_hub import snapshot_download

    start = time.monotonic()
    path = snapshot_download(
        repo_id=config.MODEL_REPO,
        revision=config.MODEL_REVISION,
        local_dir=config.MODEL_DIR,
    )
    weights_volume.commit()

    total_bytes = 0
    files = []
    for root, _, names in os.walk(path):
        for name in names:
            full = os.path.join(root, name)
            if "/.cache/" in full:
                continue
            total_bytes += os.path.getsize(full)
            files.append(os.path.relpath(full, path))

    return {
        "path": path,
        "files": len(files),
        "gib": round(total_bytes / 2**30, 2),
        "seconds": round(time.monotonic() - start, 1),
    }
