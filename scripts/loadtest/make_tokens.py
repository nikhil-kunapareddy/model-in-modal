"""Write session tokens + the load-test key for k6 into results/secrets.json (mode 0600).

k6 can't solve Turnstile, so the load test gets ordinary session tokens signed
locally with the gateway's SESSION_SIGNING_KEY (from ./.env), exactly as
/api/session would issue them. Nothing secret is printed. results/ is git-ignored.

Usage: .venv/bin/python scripts/loadtest/make_tokens.py [count]
"""

import json
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from backend import sessions  # noqa: E402


def main() -> None:
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 80
    env = {}
    for line in (ROOT / ".env").read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    if len(env.get("LOADTEST_KEY", "")) < 32:
        sys.exit("LOADTEST_KEY missing from .env: run scripts/create_loadtest_secret.sh first")
    key = env["SESSION_SIGNING_KEY"].encode()
    out = ROOT / "scripts/loadtest/results"
    out.mkdir(parents=True, exist_ok=True)
    path = out / "secrets.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"tokens": [sessions.issue(key).token for _ in range(count)],
                   "loadtest_key": env["LOADTEST_KEY"]}, f)
    print(f"wrote {count} session tokens (valid 24 h) + load-test key to {path.relative_to(ROOT)} (0600)")


if __name__ == "__main__":
    main()
