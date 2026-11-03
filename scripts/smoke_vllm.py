"""Smoke test for the deployed vLLM endpoint. Reads secrets from ./.env, never prints them.

Usage:
    .venv/bin/python scripts/smoke_vllm.py https://<workspace>--model-in-modal-vllm.modal.run

Checks, in order:
  1. No proxy token            -> 401 from Modal's proxy
  2. Cold start                -> time until /health returns 200 (with proxy token)
  3. Proxy token, no vLLM key  -> 401 from vLLM
  4. Full auth /v1/models      -> 200, lists "qwen"
  5. N streaming chat requests -> time to first token, output tokens, tokens/sec
"""

import json
import pathlib
import sys
import time

import httpx

ROOT = pathlib.Path(__file__).resolve().parent.parent
PROMPTS = [
    "Explain in about 150 words why the sky is blue.",
    "Write a short Python function that checks whether a string is a palindrome, then explain it briefly.",
    "Give me five tips for staying focused while studying, one sentence each.",
]


def load_env() -> dict[str, str]:
    env = {}
    for line in (ROOT / ".env").read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def stream_chat(client: httpx.Client, url: str, headers: dict, prompt: str) -> dict:
    body = {
        "model": "qwen",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 512,
        "temperature": 0.7,
        "top_p": 0.8,
        "top_k": 20,
        "presence_penalty": 1.5,
        "stream": True,
        "stream_options": {"include_usage": True},
        "chat_template_kwargs": {"enable_thinking": False},
    }
    t0 = time.perf_counter()
    ttft = None
    text = []
    usage = None
    with client.stream("POST", f"{url}/v1/chat/completions", headers=headers, json=body) as r:
        if r.status_code != 200:
            return {"status": r.status_code, "error": r.read().decode()[:200]}
        for line in r.iter_lines():
            if not line.startswith("data: "):
                continue
            data = line[len("data: "):]
            if data == "[DONE]":
                break
            chunk = json.loads(data)
            if chunk.get("usage"):
                usage = chunk["usage"]
            for choice in chunk.get("choices", []):
                delta = choice.get("delta", {}).get("content")
                if delta:
                    if ttft is None:
                        ttft = time.perf_counter() - t0
                    text.append(delta)
    total = time.perf_counter() - t0
    out_tokens = (usage or {}).get("completion_tokens", 0)
    decode_s = total - (ttft or 0)
    full = "".join(text)
    return {
        "status": 200,
        "ttft_s": round(ttft or 0, 3),
        "total_s": round(total, 2),
        "prompt_tokens": (usage or {}).get("prompt_tokens"),
        "output_tokens": out_tokens,
        "decode_tok_per_s": round((out_tokens - 1) / decode_s, 1) if out_tokens > 1 and decode_s > 0 else None,
        "has_think_tag": "<think>" in full,
        "preview": full[:80].replace("\n", " "),
    }


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    url = sys.argv[1].rstrip("/")
    env = load_env()
    proxy = {"Modal-Key": env["MODAL_PROXY_KEY"], "Modal-Secret": env["MODAL_PROXY_SECRET"]}
    full = {**proxy, "Authorization": f"Bearer {env['VLLM_API_KEY']}"}
    ok = True

    with httpx.Client(timeout=httpx.Timeout(900, connect=30), follow_redirects=False) as c:
        r = c.get(f"{url}/health")
        print(f"1. no proxy token        -> {r.status_code} (expect 401)")
        ok &= r.status_code == 401

        t0 = time.perf_counter()
        attempts = 0
        while True:
            attempts += 1
            try:
                r = c.get(f"{url}/health", headers=proxy)
                if r.status_code == 200:
                    break
                print(f"   /health attempt {attempts}: {r.status_code} after {time.perf_counter() - t0:.0f}s")
            except httpx.HTTPError as e:
                print(f"   /health attempt {attempts}: {type(e).__name__} after {time.perf_counter() - t0:.0f}s")
            if time.perf_counter() - t0 > 900:
                print("   gave up after 900s")
                return 1
            time.sleep(5)
        print(f"2. cold start (client)   -> healthy after {time.perf_counter() - t0:.1f}s, {attempts} attempt(s)")

        r = c.get(f"{url}/v1/models", headers=proxy)
        print(f"3. proxy only, no key    -> {r.status_code} (expect 401)")
        ok &= r.status_code == 401

        r = c.get(f"{url}/v1/models", headers=full)
        models = [m["id"] for m in r.json().get("data", [])] if r.status_code == 200 else []
        print(f"4. full auth /v1/models  -> {r.status_code} {models}")
        ok &= r.status_code == 200 and "qwen" in models

        for i, p in enumerate(PROMPTS, 1):
            res = stream_chat(c, url, full, p)
            print(f"5.{i} stream             -> {json.dumps(res)}")
            ok &= res.get("status") == 200 and not res.get("has_think_tag")

    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
