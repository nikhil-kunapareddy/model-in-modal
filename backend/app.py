"""Modal App "model-in-modal": registers every function and class.

Deploy:   .venv/bin/modal deploy -m backend.app
One-offs: .venv/bin/modal run -m backend.app::download_weights   (CPU)
          .venv/bin/modal run -m backend.app::check_vllm_flags  (~1 L4-minute)
"""

import modal

from backend import config
from backend.vllm_server import VLLMServer, check_vllm_flags  # noqa: F401
from backend.vllm_server import app as vllm_app
from backend.weights import download_weights  # noqa: F401
from backend.weights import app as weights_app

app = modal.App(config.APP_NAME)
app.include(weights_app)
app.include(vllm_app)
