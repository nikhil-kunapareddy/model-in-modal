"""Drive Safari through every state of the chat page (W3C WebDriver via safaridriver).

One-time prerequisite: Safari > Settings > Advanced > "Show features for web
developers", then Settings > Developer > "Allow remote automation".

Serve the page first, e.g.  python3 -m http.server 8080 --bind 127.0.0.1 --directory web

Scenarios:
    offline         app stopped            -> offline banner + Retry, input disabled
    turnstile-fail  page uses Cloudflare's always-fail test sitekey -> verify banner
    full            WAKES A GPU. Session, warming_up, streaming, 401 re-auth, Stop,
                    429 countdown, then redeploys with temporary config overrides
                    for busy and credits_exhausted (config.py is restored after each).
    public-credits  --prod: forced credits_exhausted on the live site, then restores
    public-chat     real Turnstile + one streamed reply on the live site (WAKES A GPU)

Usage: .venv/bin/python scripts/browser_check.py <scenario> --page http://localhost:8080/ [--out DIR]
Prints no secrets. Screenshots are written to --out.
"""

import argparse
import base64
import json
import os
import pathlib
import re
import subprocess
import sys
import time

import httpx

ROOT = pathlib.Path(__file__).resolve().parent.parent
MODAL = str(ROOT / ".venv/bin/modal")
DEPLOY_DEV = "1"  # --prod switches temporary redeploys to production mode
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}", flush=True)
    return ok


class Safari:
    """Just enough W3C WebDriver for these checks."""

    def __init__(self, port: int = 4444):
        self.base = f"http://127.0.0.1:{port}"
        self.proc = subprocess.Popen(["safaridriver", "-p", str(port)],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.http = httpx.Client(timeout=60)
        for _ in range(40):
            try:
                self.http.get(f"{self.base}/status")
                break
            except httpx.HTTPError:
                time.sleep(0.25)
        r = self.http.post(f"{self.base}/session",
                           json={"capabilities": {"alwaysMatch": {"browserName": "safari"}}})
        value = r.json()["value"]
        if "sessionId" not in value:
            self.proc.terminate()
            sys.exit(f"Could not start Safari: {value.get('message')}")
        self.sid = value["sessionId"]

    def cmd(self, method: str, path: str, body: dict | None = None):
        r = self.http.request(method, f"{self.base}/session/{self.sid}{path}", json=body)
        value = r.json().get("value")
        if r.status_code >= 400:
            raise RuntimeError(f"webdriver {path}: {value}")
        return value

    def goto(self, url: str) -> None:
        self.cmd("POST", "/url", {"url": url})

    def js(self, script: str, *args):
        return self.cmd("POST", "/execute/sync", {"script": script, "args": list(args)})

    def size(self, width: int, height: int) -> None:
        self.cmd("POST", "/window/rect", {"width": width, "height": height})

    def screenshot(self, path: pathlib.Path) -> None:
        path.write_bytes(base64.b64decode(self.cmd("GET", "/screenshot")))
        print(f"      screenshot {path}", flush=True)

    def wait(self, predicate_js: str, timeout: float, interval: float = 0.5, *args):
        end = time.time() + timeout
        while time.time() < end:
            value = self.js(predicate_js, *args)
            if value:
                return value
            time.sleep(interval)
        return None

    def quit(self) -> None:
        try:
            self.http.delete(f"{self.base}/session/{self.sid}")
        finally:
            self.proc.terminate()


# ---------------------------------------------------------------- page probes
BANNER = """const b = document.getElementById('banner');
return {hidden: b.hidden, reason: b.dataset.reason || '', kind: b.dataset.kind || '',
        text: document.getElementById('banner-text').textContent,
        action: document.getElementById('banner-action').hidden ? '' : document.getElementById('banner-action').textContent};"""
CONTROLS = """const i = document.getElementById('input'), s = document.getElementById('send');
return {inputDisabled: i.disabled, inputValue: i.value, send: s.textContent, sendDisabled: s.disabled};"""
BUBBLES = """return [...document.querySelectorAll('.msg')].map(m => ({role: m.classList[1],
        status: m.dataset.status || '', text: m.querySelector('.text').textContent,
        note: m.querySelector('.status').textContent}));"""
HAS_SESSION = "try { return !!JSON.parse(sessionStorage.getItem('mim.session')).token } catch (e) { return false }"
LAST_BOT = "const b = [...document.querySelectorAll('.msg.assistant')].pop(); return b ? {status: b.dataset.status || '', text: b.querySelector('.text').textContent, note: b.querySelector('.status').textContent} : null;"


def type_and_send(s: Safari, text: str) -> None:
    s.js("""const i = document.getElementById('input'); i.value = arguments[0];
            i.dispatchEvent(new Event('input', {bubbles: true}));
            document.getElementById('send').click();""", text)


def banner_is(s: Safari, reason: str, timeout: float = 20):
    return s.wait(f"const b = document.getElementById('banner'); return !b.hidden && b.dataset.reason === {json.dumps(reason)};", timeout)


def deploy_with(overrides: dict[str, str], ready=None) -> None:
    """Stop the app, deploy (dev mode) with temporary config.py overrides, restore the file.

    Stopping first guarantees no container from the previous version keeps
    answering. `ready(status_json)` is polled on /api/status until it is true.
    """
    path = ROOT / "backend/config.py"
    original = path.read_text()
    text = original
    for name, value in overrides.items():  # no overrides = plain redeploy
        text, n = re.subn(rf"^{name} = .*$", f"{name} = {value}  # TEMPORARY browser_check override", text, flags=re.M)
        assert n == 1, name
    path.write_text(text)
    try:
        print(f"      stop + deploy with {overrides} ...", flush=True)
        subprocess.run([MODAL, "app", "stop", "model-in-modal", "-y"], capture_output=True, cwd=ROOT)
        subprocess.run([MODAL, "deploy", "-m", "backend.app"], env={**os.environ, "MODEL_IN_MODAL_DEV": DEPLOY_DEV},
                       check=True, capture_output=True, cwd=ROOT)
    finally:
        path.write_text(original)
    sys.path.insert(0, str(ROOT))
    from backend import config
    for _ in range(60):
        try:
            status = httpx.get(f"{config.GATEWAY_BASE_URL}/api/status", timeout=30).json()
            if ready is None or ready(status):
                return
        except (httpx.HTTPError, ValueError):
            pass
        time.sleep(2)
    raise RuntimeError("new deployment never became ready")


# ---------------------------------------------------------------- scenarios
def scenario_offline(s: Safari, page: str, out: pathlib.Path) -> None:
    s.goto(page)
    check("offline banner shown", bool(banner_is(s, "offline")), json.dumps(s.js(BANNER)))
    b, c = s.js(BANNER), s.js(CONTROLS)
    check("offline banner has Retry", b["action"] == "Retry" and b["kind"] == "error")
    check("input disabled while offline", c["inputDisabled"] and c["sendDisabled"])
    s.screenshot(out / "offline.png")


def scenario_turnstile_fail(s: Safari, page: str, out: pathlib.Path) -> None:
    s.goto(page)
    ok = banner_is(s, "turnstile", timeout=40)
    check("turnstile failure banner", bool(ok), json.dumps(s.js(BANNER)))
    check("no session stored", not s.js(HAS_SESSION))
    check("banner offers Try again", s.js(BANNER)["action"] == "Try again")
    s.screenshot(out / "turnstile-fail.png")


def wait_reply(s: Safari, timeout: float, shot: pathlib.Path | None = None) -> dict | None:
    """Wait for the last assistant bubble to finish; screenshot once if it shows 'waiting'."""
    end, shot_taken, saw_waiting = time.time() + timeout, False, False
    while time.time() < end:
        bot = s.js(LAST_BOT)
        if bot and bot["status"] == "waiting":
            saw_waiting = True
            if shot and not shot_taken:
                time.sleep(2)
                s.screenshot(shot)
                print(f"      waiting note: {s.js(LAST_BOT)['note']}", flush=True)
                shot_taken = True
        if bot and bot["status"] in ("done", "error", "stopped"):
            bot["saw_waiting"] = saw_waiting
            return bot
        if not bot:  # turn was undone (e.g. 429)
            return None
        time.sleep(1)
    return {"status": "timeout", "saw_waiting": saw_waiting}


def scenario_full(s: Safari, page: str, out: pathlib.Path) -> None:
    # 1. Load: status ok, Turnstile passes, session stored (this wakes a GPU).
    t0 = time.time()
    s.goto(page)
    check("session created via Turnstile", bool(s.wait(HAS_SESSION, 40)), f"{time.time() - t0:.1f}s")
    c = s.js(CONTROLS)
    check("input enabled", not c["inputDisabled"])
    s.size(420, 860)
    s.screenshot(out / "mobile-empty.png")
    s.size(1100, 900)

    # 2. First message: warming_up with timer, then a streamed reply.
    t1 = time.time()
    type_and_send(s, "Say hello in five words.")
    bot = wait_reply(s, 26 * 60, shot=out / "warming.png")
    check("first reply streams", bot and bot["status"] == "done" and bot["text"],
          f"{time.time() - t1:.0f}s, waited={bot and bot.get('saw_waiting')}, text={len((bot or {}).get('text', ''))} chars")
    s.screenshot(out / "first-reply.png")

    # 3. 401: corrupt the stored token -> page re-runs Turnstile and retries once.
    s.js("""const k = 'mim.session'; const v = JSON.parse(sessionStorage.getItem(k));
            v.token = 'v1.bogus.9999999999.AAAA'; sessionStorage.setItem(k, JSON.stringify(v));""")
    s.goto(page)  # the page now starts with the bogus token from sessionStorage
    s.wait("return !document.getElementById('input').disabled;", 30)
    type_and_send(s, "Name one planet.")
    bot = wait_reply(s, 120)
    token_ok = s.js("try { return !JSON.parse(sessionStorage.getItem('mim.session')).token.includes('bogus') } catch (e) { return false }")
    check("401 -> re-auth and retry", bool(bot and bot["status"] == "done" and token_ok), json.dumps(bot)[:120])

    # 4. Stop mid-stream.
    type_and_send(s, "Count from 1 to 300, one number per line.")
    s.wait("const b = [...document.querySelectorAll('.msg.assistant')].pop(); return b && b.dataset.status === 'streaming' && b.querySelector('.text').textContent.length > 30;", 60, 0.2)
    s.js("document.getElementById('send').click();")  # the button reads "Stop" while busy
    bot = wait_reply(s, 30)
    check("Stop keeps partial reply", bool(bot and bot["status"] == "stopped" and bot["text"]), f"{len((bot or {}).get('text', ''))} chars")
    # The gateway frees the slot once it notices the disconnect; a message sent in that
    # same instant gets the (correct) 3 s "wait for the current response" countdown.
    time.sleep(2)

    # 5. 429: keep sending until the per-minute limit trips; countdown must tick.
    sent, other_429s = 0, []
    for _ in range(16):
        s.wait("const b = document.getElementById('banner'); return b.hidden || b.dataset.reason !== 'cooldown';", 70)
        type_and_send(s, "Reply with one word: ok")
        s.wait("return document.getElementById('send').textContent === 'Send';", 60, 0.2)
        banner = s.js(BANNER)
        if banner["reason"] == "cooldown":
            if "minute" in banner["text"]:
                break
            other_429s.append(banner["text"])
            continue
        sent += 1
    check("no false 'one at a time' 429 between quick messages", not other_429s, json.dumps(other_429s))
    banner = s.js(BANNER)
    c = s.js(CONTROLS)
    check("429 shows countdown banner", banner["reason"] == "cooldown" and "minute" in banner["text"] and "send again in" in banner["text"],
          f"after {sent} sends: {banner['text']}")
    check("429 restores the typed message", c["inputValue"] == "Reply with one word: ok" and c["sendDisabled"])
    s.screenshot(out / "rate-limited.png")
    first = banner["text"]
    time.sleep(2.5)
    check("countdown ticks", s.js(BANNER)["text"] != first, s.js(BANNER)["text"])
    cleared = s.wait("return document.getElementById('banner').hidden;", 70)
    check("countdown clears and send re-enables", bool(cleared) and not s.js(CONTROLS)["sendDisabled"])

    # 6. busy: redeploy with the global cap at 0; reload keeps the session (no new GPU wake).
    deploy_with({"GLOBAL_MAX_STREAMS": "0"}, ready=lambda st: st.get("available") is True)
    s.goto(page)
    s.wait("return !document.getElementById('input').disabled;", 30)
    check("session reused after reload", bool(s.js(HAS_SESSION)))
    type_and_send(s, "Hello?")
    check("busy banner", bool(banner_is(s, "busy", 30)), s.js(BANNER)["text"])
    s.screenshot(out / "busy.png")

    # 7. credits_exhausted mid-chat, then on a fresh load.
    deploy_with({"SOFT_CAP_USD": "0.5"}, ready=lambda st: st.get("reason") == "credits_exhausted")
    type_and_send(s, "Hello again?")
    ok = banner_is(s, "credits", 40)
    check("credits banner mid-chat", bool(ok), s.js(BANNER)["text"])
    check("input disabled when out of credits", s.js(CONTROLS)["inputDisabled"])
    s.goto(page)
    ok = banner_is(s, "credits", 30)
    check("credits banner on load (with reset date)", bool(ok) and "back on" in s.js(BANNER)["text"], s.js(BANNER)["text"])
    s.screenshot(out / "credits.png")
    s.size(420, 860)
    s.screenshot(out / "mobile-credits.png")


def scenario_public_credits(s: Safari, page: str, out: pathlib.Path) -> None:
    """Forced credits_exhausted on the public site; always restores the normal deploy."""
    try:
        deploy_with({"SOFT_CAP_USD": "0.5"}, ready=lambda st: st.get("reason") == "credits_exhausted")
        s.goto(page)
        ok = banner_is(s, "credits", 40)
        check("public: credits banner on load", bool(ok) and "back on" in s.js(BANNER)["text"], s.js(BANNER)["text"])
        check("public: input disabled", s.js(CONTROLS)["inputDisabled"])
        check("public: no session created (no Turnstile, no GPU wake)", not s.js(HAS_SESSION))
        s.screenshot(out / "public-credits.png")
        s.size(420, 860)
        s.screenshot(out / "public-credits-mobile.png")
    finally:
        deploy_with({}, ready=lambda st: st.get("available") is True)
    check("public: normal cap restored (status available)", True)


def scenario_public_chat(s: Safari, page: str, out: pathlib.Path) -> None:
    """Real Turnstile + one streamed reply on the public site. WAKES A GPU."""
    s.goto(page)
    got = s.wait(HAS_SESSION, 60)
    if not check("public: real Turnstile issued a session", bool(got), json.dumps(s.js(BANNER))):
        s.screenshot(out / "public-turnstile.png")
        return
    t0 = time.time()
    type_and_send(s, "Say hello in five words.")
    bot = wait_reply(s, 26 * 60, shot=out / "public-warming.png")
    check("public: reply streams", bool(bot and bot["status"] == "done" and bot["text"]),
          f"{time.time() - t0:.0f}s, waited={bot and bot.get('saw_waiting')}")
    s.screenshot(out / "public-reply.png")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("scenario", choices=["offline", "turnstile-fail", "full", "public-credits", "public-chat"])
    parser.add_argument("--prod", action="store_true", help="temporary redeploys use production mode")
    parser.add_argument("--page", default="http://localhost:8080/")
    parser.add_argument("--out", default=str(ROOT / "browser-check"))
    args = parser.parse_args()
    global DEPLOY_DEV
    DEPLOY_DEV = "0" if args.prod else "1"
    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    s = Safari()
    try:
        s.size(1100, 900)
        {"offline": scenario_offline, "turnstile-fail": scenario_turnstile_fail, "full": scenario_full,
         "public-credits": scenario_public_credits, "public-chat": scenario_public_chat}[args.scenario](s, args.page, out)
    finally:
        s.quit()
    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed" + (f"; failed: {failed}" if failed else ""))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
