"""Gateway end-to-end in-process: FastAPI TestClient + fake Redis + mocked vLLM/Turnstile."""

import asyncio
import dataclasses
import json
import logging
import time
import urllib.parse

import httpx
import pytest
from fakes import FakeRedis
from fastapi.testclient import TestClient
from starlette.requests import Request

from backend import config, gateway, meter, ratelimit, sessions

KEY = b"k" * 32
SETTINGS = gateway.Settings(
    signing_key=KEY,
    turnstile_secret="turnstile-secret-value",
    vllm_api_key="vllm-key-value",
    proxy_key="wk-proxykey",
    proxy_secret="ws-proxysecret",
    vllm_url="https://vllm.test",
)
SECRETS = ["turnstile-secret-value", "vllm-key-value", "wk-proxykey", "ws-proxysecret"]
VISITOR_IP = "203.0.113.7"


def sse_body(*chunks):
    return "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"


SSE_OK = sse_body(
    {"choices": [{"delta": {"role": "assistant", "content": ""}}]},
    {"choices": [{"delta": {"content": "Hel"}}]},
    {"choices": [{"delta": {"content": "lo"}, "finish_reason": "stop"}]},
    {"choices": [], "usage": {"prompt_tokens": 20, "completion_tokens": 2, "total_tokens": 22}},
)


def stream_response(body=SSE_OK):
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body.encode())


class Harness:
    def __init__(self, *, spend=1.0, turnstile_ok=True, dev=False, chat=None, health=None):
        self.spend_value = spend
        self.turnstile_ok = turnstile_ok
        self.chat_handler = chat or (lambda request: stream_response())
        self.health_handler = health or (lambda request: httpx.Response(200))
        self.redis = FakeRedis()
        self.upstream_calls: list[httpx.Request] = []
        self.turnstile_calls: list[httpx.Request] = []
        settings = dataclasses.replace(SETTINGS, dev=dev)
        self.upstream = gateway.Upstream(httpx.AsyncClient(transport=httpx.MockTransport(self._upstream)), settings)
        self.limiter = ratelimit.RateLimiter(self.redis, ip_key=KEY)
        self.app = gateway.create_app(
            settings=settings,
            limiter=self.limiter,
            spend=meter.SpendCache(fetch=self._spend, ttl=0),
            upstream=self.upstream,
            turnstile_client=httpx.AsyncClient(transport=httpx.MockTransport(self._turnstile)),
        )

    async def _spend(self):
        if isinstance(self.spend_value, Exception):
            raise self.spend_value
        return self.spend_value

    async def _upstream(self, request):
        self.upstream_calls.append(request)
        handler = self.health_handler if request.url.path == "/health" else self.chat_handler
        result = handler(request)
        return await result if asyncio.iscoroutine(result) else result

    def _turnstile(self, request):
        self.turnstile_calls.append(request)
        if self.turnstile_ok:
            return httpx.Response(200, json={"success": True})
        return httpx.Response(200, json={"success": False, "error-codes": ["invalid-input-response"]})

    # helpers
    def chat_calls(self):
        return [c for c in self.upstream_calls if c.url.path == "/v1/chat/completions"]

    def health_calls(self):
        return [c for c in self.upstream_calls if c.url.path == "/health"]

    def chat(self, token, content="hi", **extra):
        body = {"messages": [{"role": "user", "content": content}], **extra}
        return self.client.post("/api/chat", json=body, headers={"Authorization": f"Bearer {token}"})


@pytest.fixture
def make():
    opened = []

    def _make(**kwargs):
        h = Harness(**kwargs)
        h.client = TestClient(h.app, client=(VISITOR_IP, 50000))
        h.client.__enter__()
        opened.append(h.client)
        return h

    yield _make
    for c in opened:
        c.__exit__(None, None, None)


@pytest.fixture
def h(make):
    return make()


def new_token():
    return sessions.issue(KEY).token


def events(response):
    return [json.loads(line[5:]) for line in response.text.splitlines() if line.startswith("data:")]


def wait_until(predicate, timeout=2.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def counter_values(h, prefix="rl:"):
    return {k: h.redis.get(k) for k in h.redis.keys() if k.startswith(prefix) and k in h.redis.strings}


# ---------------------------------------------------------------- status
def test_status_available(h):
    r = h.client.get("/api/status")
    assert r.status_code == 200
    assert r.json() == {"available": True, "reason": None, "resets_at": meter.resets_at()}


def test_status_credits_exhausted_does_not_leak_spend(make):
    h = make(spend=config.SOFT_CAP_USD)
    body = h.client.get("/api/status").json()
    assert body == {"available": False, "reason": "credits_exhausted", "resets_at": meter.resets_at()}
    assert "27" not in json.dumps(body)


def test_status_fails_closed_when_meter_unreadable(make):
    h = make(spend=RuntimeError("dict down"))
    assert h.client.get("/api/status").json()["reason"] == "unavailable"


# ---------------------------------------------------------------- session
def test_session_success_issues_token_and_wakes_model(h):
    r = h.client.post("/api/session", json={"turnstile_token": "XXXX.DUMMY.TOKEN.XXXX"})
    assert r.status_code == 200
    body = r.json()
    assert sessions.verify(body["token"], KEY) is not None
    assert body["expires_at"].endswith("Z")

    form = dict(urllib.parse.parse_qsl(h.turnstile_calls[0].content.decode()))
    assert form == {"secret": "turnstile-secret-value", "response": "XXXX.DUMMY.TOKEN.XXXX", "remoteip": VISITOR_IP}

    assert wait_until(lambda: h.health_calls())
    ping = h.health_calls()[0]
    assert ping.headers["Modal-Key"] == "wk-proxykey"
    assert ping.headers["Modal-Secret"] == "ws-proxysecret"
    assert ping.headers["Authorization"] == "Bearer vllm-key-value"
    assert wait_until(h.upstream.is_warm)


def test_wake_follows_modal_redirect_with_auth_headers(make):
    # Modal answers a still-queued request with 303 -> same URL + call id; following
    # it resumes the same queued request.
    def health(request):
        if "__modal_function_call_id" not in str(request.url):
            return httpx.Response(303, headers={"location": "https://vllm.test/health?__modal_function_call_id=fc-1"})
        return httpx.Response(200)

    h = make(health=health)
    h.client.post("/api/session", json={"turnstile_token": "XXXX.DUMMY.TOKEN.XXXX"})
    assert wait_until(h.upstream.is_warm)
    first, second = h.health_calls()[:2]
    assert "__modal_function_call_id=fc-1" in str(second.url)
    for header in ("Modal-Key", "Modal-Secret", "Authorization"):
        assert second.headers[header] == first.headers[header]


def test_session_turnstile_failure(make):
    h = make(turnstile_ok=False)
    r = h.client.post("/api/session", json={"turnstile_token": "bad"})
    assert r.status_code == 403
    assert r.json()["error"] == "turnstile_failed"
    time.sleep(0.05)
    assert h.health_calls() == []


@pytest.mark.parametrize(
    "kwargs",
    [{"json": {}}, {"json": {"turnstile_token": ""}}, {"json": {"turnstile_token": 5}},
     {"json": {"turnstile_token": "x" * 2049}}, {"content": b"not json"}],
)
def test_session_bad_body(h, kwargs):
    r = h.client.post("/api/session", **kwargs)
    assert r.status_code == 400
    assert set(r.json()) == {"error", "message"}
    assert h.turnstile_calls == []


def test_session_refused_when_credits_exhausted(make):
    h = make(spend=30.0)
    r = h.client.post("/api/session", json={"turnstile_token": "XXXX.DUMMY.TOKEN.XXXX"})
    assert r.status_code == 503 and r.json()["error"] == "credits_exhausted"
    assert r.json()["resets_at"] == meter.resets_at()
    assert h.turnstile_calls == [] and h.upstream_calls == []


def test_session_body_too_large(h):
    r = h.client.post("/api/session", content=b"x" * (config.MAX_BODY_BYTES + 1))
    assert r.status_code == 413 and r.json()["error"] == "request_too_large"


# ---------------------------------------------------------------- chat: auth + budget
@pytest.mark.parametrize("auth", [None, "Bearer", "Bearer garbage", "Basic abc"])
def test_chat_requires_valid_token(h, auth):
    headers = {"Authorization": auth} if auth else {}
    r = h.client.post("/api/chat", json={"messages": [{"role": "user", "content": "hi"}]}, headers=headers)
    assert r.status_code == 401
    assert r.json() == {"error": "unauthorized", "message": "Session expired or invalid."}
    assert h.redis.commands == 0 and h.upstream_calls == []


def test_chat_rejects_expired_token(h):
    old = sessions.issue(KEY, now=time.time() - config.SESSION_TTL_S - 1).token
    assert h.chat(old).status_code == 401


def test_chat_credits_exhausted_never_reaches_redis_or_gpu(make):
    h = make(spend=config.SOFT_CAP_USD + 0.01)
    r = h.chat(new_token())
    assert r.status_code == 503
    assert r.json() == {"error": "credits_exhausted", "message": "This month's free credit is used up.",
                        "resets_at": meter.resets_at()}
    assert h.redis.commands == 0 and h.upstream_calls == []


# ---------------------------------------------------------------- chat: streaming
def test_chat_streams_and_shapes_request(h):
    r = h.chat(new_token(), content="hello there", model="gpt-4", max_tokens=99999, temperature=2)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    assert events(r) == [
        {"type": "delta", "text": "Hel"},
        {"type": "delta", "text": "lo"},
        {"type": "done", "finish_reason": "stop", "output_tokens": 2},
    ]

    call = h.chat_calls()[0]
    assert str(call.url) == "https://vllm.test/v1/chat/completions"
    assert call.headers["Modal-Key"] == "wk-proxykey"
    assert call.headers["Modal-Secret"] == "ws-proxysecret"
    assert call.headers["Authorization"] == "Bearer vllm-key-value"
    sent = json.loads(call.content)
    assert sent["model"] == "qwen" and sent["max_tokens"] == 512 and sent["temperature"] == config.TEMPERATURE
    assert sent["messages"][0]["role"] == "system"
    assert sent["chat_template_kwargs"] == {"enable_thinking": False}

    # Stream slots are released, the message stays counted.
    assert h.redis.zsets == {}
    assert sorted(counter_values(h).values()) == [1, 1, 1]
    assert h.upstream.is_warm()


def test_stream_slot_is_released_before_the_done_event():
    """A visitor who replies the instant a reply ends must not get a false 429."""
    h = Harness()
    order = []
    real_release = h.limiter.release

    async def recording_release(ticket, refund=False):
        order.append("release")
        await real_release(ticket, refund)

    h.limiter.release = recording_release
    body = json.dumps({"messages": [{"role": "user", "content": "hi"}]}).encode()
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST",
        "scheme": "http", "path": "/api/chat", "raw_path": b"/api/chat", "query_string": b"",
        "root_path": "", "client": (VISITOR_IP, 1), "server": ("test", 80),
        "headers": [(b"host", b"test"), (b"content-type", b"application/json"),
                    (b"authorization", f"Bearer {new_token()}".encode()),
                    (b"content-length", str(len(body)).encode())],
    }
    delivered = False

    async def receive():
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}
        await asyncio.sleep(3600)  # the browser never disconnects

    async def send(message):
        if message["type"] == "http.response.body" and b'"type": "done"' in message.get("body", b""):
            order.append("done-event")

    asyncio.run(h.app(scope, receive, send))
    assert order == ["release", "done-event"]


def test_mid_stream_error_is_reported(make):
    body = sse_body({"choices": [{"delta": {"content": "Hi"}}]}, {"error": {"message": "boom", "code": 500}})
    h = make(chat=lambda request: stream_response(body))
    r = h.chat(new_token())
    assert [e["type"] for e in events(r)] == ["delta", "error"]
    assert events(r)[1]["error"] == "upstream_error"
    assert h.redis.zsets == {}


# ---------------------------------------------------------------- chat: limits
def test_chat_per_minute_limit_returns_429_with_retry_after(h):
    token = new_token()
    statuses = [h.chat(token).status_code for _ in range(config.SESSION_MSGS_PER_MIN)]
    assert statuses == [200] * config.SESSION_MSGS_PER_MIN
    r = h.chat(token)
    if r.status_code == 200:  # the minute rolled over mid-test; one more fills it
        statuses.append(200)
        r = h.chat(token)
    assert r.status_code == 429
    body = r.json()
    assert body["error"] == "rate_limited" and 1 <= body["retry_after"] <= 60
    assert r.headers["Retry-After"] == str(body["retry_after"])


def test_chat_one_stream_per_session(h):
    s = sessions.issue(KEY)
    h.redis.zadd(f"rl:s:{s.session_id}:streams", {"other-stream": time.time()})
    r = h.chat(s.token)
    assert r.status_code == 429 and r.json()["retry_after"] == config.STREAM_IN_PROGRESS_RETRY_AFTER_S
    assert h.chat_calls() == []


def test_chat_busy_when_global_cap_reached(h):
    now = time.time()
    h.redis.zadd(ratelimit.GLOBAL_STREAMS_KEY, {f"m{i}": now for i in range(config.GLOBAL_MAX_STREAMS)})
    r = h.chat(new_token())
    assert r.status_code == 503
    assert r.json()["error"] == "busy" and r.json()["retry_after"] == config.BUSY_RETRY_AFTER_S
    assert h.chat_calls() == []
    assert set(counter_values(h).values()) == {0}  # refunded


def test_chat_fails_closed_when_redis_down(h):
    h.redis.fail = True
    r = h.chat(new_token())
    assert r.status_code == 503 and r.json()["error"] == "unavailable"
    assert h.chat_calls() == []


# ---------------------------------------------------------------- chat: shaping errors
def test_chat_bad_body_releases_slot(h):
    token = new_token()
    r = h.client.post("/api/chat", json={"messages": [{"role": "user", "content": "x" * 4001}]},
                      headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 400 and r.json()["error"] == "message_too_long"
    assert h.redis.zsets == {} and h.chat_calls() == []
    r = h.client.post("/api/chat", content=b"{", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 400 and r.json()["error"] == "invalid_request"


def test_chat_body_too_large(h):
    r = h.client.post("/api/chat", content=b"x" * (config.MAX_BODY_BYTES + 1),
                      headers={"Authorization": f"Bearer {new_token()}"})
    assert r.status_code == 413
    assert h.redis.zsets == {}


# ---------------------------------------------------------------- chat: cold model
def test_chat_warming_up_refunds_and_wakes_then_skips_redis(make, monkeypatch):
    monkeypatch.setattr(config, "UPSTREAM_WARM_WAIT_S", 0.05)

    async def slow(request):
        await asyncio.sleep(30)
        return httpx.Response(200)

    h = make(chat=slow, health=slow)
    token = new_token()
    r = h.chat(token)
    assert r.status_code == 503
    assert r.json() == {"error": "warming_up", "message": "Waking up the model. This can take a few minutes.",
                        "retry_after": config.WARMING_RETRY_AFTER_S}
    assert set(counter_values(h).values()) == {0}  # refunded
    assert h.redis.zsets == {}
    assert wait_until(lambda: h.health_calls())  # wake-up ping started
    assert h.upstream.waking

    before = h.redis.commands
    r = h.chat(token)
    assert r.status_code == 503 and r.json()["error"] == "warming_up"
    assert h.redis.commands == before  # answered without Redis or GPU


def test_chat_modal_redirect_means_warming_up(make):
    h = make(chat=lambda request: httpx.Response(303, headers={"location": "https://vllm.test/x"}),
             health=lambda request: httpx.Response(303))
    r = h.chat(new_token())
    assert r.status_code == 503 and r.json()["error"] == "warming_up"
    assert set(counter_values(h).values()) == {0}


def test_chat_upstream_error_is_502_and_refunded(make):
    h = make(chat=lambda request: httpx.Response(500, text="internal"))
    r = h.chat(new_token())
    assert r.status_code == 502 and r.json()["error"] == "upstream_error"
    assert set(counter_values(h).values()) == {0}
    assert h.redis.zsets == {}


# ---------------------------------------------------------------- CORS
def preflight(h, origin):
    return h.client.options("/api/chat", headers={
        "Origin": origin, "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "authorization,content-type"})


@pytest.mark.parametrize("origin", ["https://modelinmodal.duckdns.org", "https://nikhil-kunapareddy.github.io"])
def test_cors_allows_site_origins(h, origin):
    r = preflight(h, origin)
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == origin
    assert "access-control-allow-credentials" not in r.headers
    assert "POST" in r.headers["access-control-allow-methods"]
    assert h.client.get("/api/status", headers={"Origin": origin}).headers["access-control-allow-origin"] == origin


@pytest.mark.parametrize("origin", ["https://evil.example", "http://modelinmodal.duckdns.org", "http://localhost:8080"])
def test_cors_blocks_other_origins(h, origin):
    r = preflight(h, origin)
    assert r.status_code == 400
    assert "access-control-allow-origin" not in r.headers


def test_cors_allows_localhost_in_dev_only(make):
    h = make(dev=True)
    assert preflight(h, "http://localhost:8080").headers["access-control-allow-origin"] == "http://localhost:8080"
    assert "access-control-allow-origin" not in preflight(h, "https://evil.example").headers


# ---------------------------------------------------------------- errors + logging
def test_unknown_routes_return_json_errors(h):
    assert h.client.get("/nope").json()["error"] == "not_found"
    assert h.client.get("/api/chat").json()["error"] == "method_not_allowed"


def test_logs_have_metadata_but_no_content_or_secrets(h, caplog):
    caplog.set_level(logging.INFO, logger="model-in-modal")
    body = sse_body({"choices": [{"delta": {"content": "PRIVATE-REPLY-456"}}]},
                    {"choices": [], "usage": {"completion_tokens": 7}})
    h.chat_handler = lambda request: stream_response(body)
    s = h.client.post("/api/session", json={"turnstile_token": "XXXX.DUMMY.TOKEN.XXXX"}).json()
    r = h.chat(s["token"], content="PRIVATE-PROMPT-123")
    assert r.status_code == 200

    text = caplog.text
    assert "chat sid=" in text and f"ip={VISITOR_IP}" in text
    assert "prompt_chars=18" in text and "output_tokens=7" in text and "latency_ms=" in text
    assert "status=200" in text
    for forbidden in ["PRIVATE-PROMPT-123", "PRIVATE-REPLY-456", s["token"], *SECRETS]:
        assert forbidden not in text


def test_settings_repr_hides_secrets():
    text = repr(SETTINGS) + str(SETTINGS)
    assert not any(secret in text for secret in SECRETS)


def test_settings_from_env_requires_long_signing_key():
    env = {"SESSION_SIGNING_KEY": "short", "TURNSTILE_SECRET": "t", "VLLM_API_KEY": "v",
           "MODAL_PROXY_KEY": "wk", "MODAL_PROXY_SECRET": "ws"}
    with pytest.raises(RuntimeError):
        gateway.Settings.from_env(env)
    env["SESSION_SIGNING_KEY"] = "a" * 64
    assert gateway.Settings.from_env(env).signing_key == b"a" * 64


# ---------------------------------------------------------------- client IP
def request_with(headers, peer="10.0.0.1"):
    scope = {"type": "http", "headers": [(k.encode(), v.encode()) for k, v in headers.items()],
             "client": (peer, 1234)}
    return Request(scope)


def test_client_ip_defaults_to_peer_and_ignores_spoofable_headers():
    assert gateway.client_ip(request_with({"x-forwarded-for": "6.6.6.6"})) == "10.0.0.1"


def test_client_ip_header_uses_rightmost_entry(monkeypatch):
    monkeypatch.setattr(config, "CLIENT_IP_HEADER", "x-forwarded-for")
    req = request_with({"x-forwarded-for": "6.6.6.6, 198.51.100.9"})
    assert gateway.client_ip(req) == "198.51.100.9"
    assert gateway.client_ip(request_with({})) == "10.0.0.1"
