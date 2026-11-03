"""End-to-end checks against the deployed gateway. Prints no secrets or tokens.

Usage:
    .venv/bin/python scripts/e2e_gateway.py --cpu-only   # never wakes the GPU
    .venv/bin/python scripts/e2e_gateway.py              # full run: wakes one L4

The full run needs the gateway secret to hold Cloudflare's always-pass TEST
Turnstile secret, so the dummy token below is accepted.
"""

import argparse
import json
import pathlib
import sys
import threading
import time

import httpx

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from backend import config  # noqa: E402

DUMMY_TURNSTILE_TOKEN = "XXXX.DUMMY.TOKEN.XXXX"  # Cloudflare's documented test token
SITE = f"https://{config.DUCKDNS_DOMAIN}"
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}", flush=True)
    return ok


def events(text: str) -> list[dict]:
    return [json.loads(line[5:]) for line in text.splitlines() if line.startswith("data:")]


def preflight(c: httpx.Client, origin: str) -> httpx.Response:
    return c.options("/api/chat", headers={
        "Origin": origin, "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "authorization,content-type"})


def cpu_checks(c: httpx.Client) -> None:
    r = c.get("/api/status", headers={"Origin": SITE, "X-Forwarded-For": "6.6.6.6", "X-Real-IP": "7.7.7.7"})
    body = r.json()
    check("status 200 + exact keys", r.status_code == 200 and set(body) == {"available", "reason", "resets_at"},
          json.dumps(body))
    check("status CORS header for site", r.headers.get("access-control-allow-origin") == SITE)

    check("preflight allowed origin", preflight(c, SITE).headers.get("access-control-allow-origin") == SITE)
    r = preflight(c, "https://evil.example")
    check("preflight other origin blocked", "access-control-allow-origin" not in r.headers, f"status {r.status_code}")
    r = preflight(c, "http://localhost:8080")
    print(f"info  localhost preflight allowed: {'access-control-allow-origin' in r.headers} (expected only in dev mode)")

    r = c.post("/api/chat", json={"messages": [{"role": "user", "content": "hi"}]})
    check("chat without token -> 401", r.status_code == 401 and r.json()["error"] == "unauthorized")
    r = c.post("/api/chat", json={"messages": []}, headers={"Authorization": "Bearer v1.fake.9999999999.sig"})
    check("chat with forged token -> 401", r.status_code == 401)
    r = c.post("/api/session", json={})
    check("session without turnstile token -> 400", r.status_code == 400 and r.json()["error"] == "invalid_request")
    r = c.get("/nope")
    check("unknown route -> JSON 404", r.status_code == 404 and r.json()["error"] == "not_found")


def chat_stream(c: httpx.Client, token: str, prompt: str, on_first_delta=None) -> dict:
    t0 = time.perf_counter()
    out = {"status": None, "ttft": None, "text_chars": 0, "events": []}
    with c.stream("POST", "/api/chat", json={"messages": [{"role": "user", "content": prompt}]},
                  headers={"Authorization": f"Bearer {token}", "Origin": SITE}, timeout=120) as r:
        out["status"] = r.status_code
        if r.status_code != 200:
            out["body"] = json.loads(r.read())
            return out
        for line in r.iter_lines():
            if not line.startswith("data:"):
                continue
            ev = json.loads(line[5:])
            out["events"].append(ev["type"])
            if ev["type"] == "delta":
                if out["ttft"] is None:
                    out["ttft"] = time.perf_counter() - t0
                    if on_first_delta:
                        on_first_delta()
                out["text_chars"] += len(ev["text"])
            elif ev["type"] == "done":
                out["done"] = ev
            elif ev["type"] == "error":
                out["error"] = ev
    out["total"] = time.perf_counter() - t0
    return out


def gpu_checks(c: httpx.Client) -> None:
    t_session = time.perf_counter()
    r = c.post("/api/session", json={"turnstile_token": DUMMY_TURNSTILE_TOKEN}, headers={"Origin": SITE})
    if not check("session with test token -> 200", r.status_code == 200, f"status {r.status_code}"):
        print(r.text[:300])
        return
    token = r.json()["token"]
    check("session expires_at is ISO UTC", r.json()["expires_at"].endswith("Z"), r.json()["expires_at"])

    # Cold start as a visitor sees it: retry on warming_up until tokens stream.
    warming = 0
    while True:
        res = chat_stream(c, token, "Reply with one short friendly sentence.")
        if res["status"] == 503 and res["body"]["error"] == "warming_up":
            warming += 1
            elapsed = time.perf_counter() - t_session
            print(f"      warming_up #{warming} at {elapsed:.0f}s (retry_after {res['body']['retry_after']})", flush=True)
            if elapsed > config.WAKE_MAX_S:
                check("model woke up", False, "gave up")
                return
            time.sleep(res["body"]["retry_after"])
            continue
        break
    cold = time.perf_counter() - t_session
    ok = res["status"] == 200 and "done" in res
    check("first chat streams after warm-up", ok,
          f"{warming} warming_up replies, ready {cold:.0f}s after session, ttft {res['ttft'] or 0:.2f}s, "
          f"{res.get('done', {}).get('output_tokens')} tokens")
    if not ok:
        print(res)
        return

    # Warm request timings.
    res = chat_stream(c, token, "Name three colors.")
    check("warm chat streams", res["status"] == 200 and "done" in res,
          f"ttft {res['ttft'] or 0:.2f}s, total {res.get('total', 0):.2f}s, {res.get('done', {}).get('output_tokens')} tokens")

    # One stream per session: a second chat while the first is streaming -> 429.
    second: dict = {}

    def fire_second():
        r2 = c.post("/api/chat", json={"messages": [{"role": "user", "content": "hi"}]},
                    headers={"Authorization": f"Bearer {token}"})
        second.update(status=r2.status_code, body=r2.json())

    long = chat_stream(c, token, "Count from 1 to 40, one number per line.",
                       on_first_delta=lambda: threading.Thread(target=fire_second).start())
    time.sleep(0.5)
    check("second concurrent stream -> 429", second.get("status") == 429, json.dumps(second.get("body")))
    check("first stream unaffected", long["status"] == 200 and "done" in long)

    # Per-minute limit: keep sending until 429 (each reply is a few tokens).
    sent = 0
    while sent < config.SESSION_MSGS_PER_MIN + 2:
        r = c.post("/api/chat", json={"messages": [{"role": "user", "content": "Say ok."}]},
                   headers={"Authorization": f"Bearer {token}"})
        if r.status_code == 429:
            break
        r.read()
        sent += 1
    body = r.json() if r.status_code == 429 else {}
    check("per-minute limit -> 429 + retry_after", r.status_code == 429 and body.get("retry_after", 0) > 0,
          f"after {sent} more messages: {json.dumps(body)}; Retry-After={r.headers.get('retry-after')}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cpu-only", action="store_true")
    parser.add_argument("--url", default=config.GATEWAY_BASE_URL)
    args = parser.parse_args()
    with httpx.Client(base_url=args.url, timeout=60) as c:
        cpu_checks(c)
        if not args.cpu_only:
            gpu_checks(c)
    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed" + (f"; failed: {failed}" if failed else ""))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
