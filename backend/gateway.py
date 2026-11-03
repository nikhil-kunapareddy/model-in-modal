"""Gateway: the only public API. FastAPI on a small CPU container.

    GET  /api/status   -> {available, reason, resets_at}
    POST /api/session  {turnstile_token} -> {token, expires_at}
    POST /api/chat     {messages} + Authorization: Bearer <token> -> text/event-stream

/api/chat checks, in order:
    1. session token                     401 unauthorized
    2. budget (spend meter)              503 credits_exhausted (+ resets_at)
       model known to be waking up       503 warming_up        (no Redis, no GPU)
    3. per-session and per-IP limits     429 rate_limited      (+ retry_after)
    4. global stream cap                 503 busy
    5. request shaping                   400
    6. forward to vLLM and stream        503 warming_up if vLLM sends no headers
                                         within UPSTREAM_WARM_WAIT_S; 502 on errors

Stream format, one JSON object per SSE `data:` line:
    {"type": "delta", "text": "..."}
    {"type": "done", "finish_reason": "stop" | "length", "output_tokens": 42, "ttft_ms": 180}
    {"type": "error", "error": "upstream_error" | "timeout", "message": "..."}

Errors are always JSON: {error, message, retry_after?, resets_at?}.
Logs carry session id, IP, prompt chars, output tokens, latency and status;
never message content or secrets.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any

import anyio
import httpx
import modal
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from backend import config, meter, ratelimit, sessions, shaping

log = logging.getLogger("model-in-modal.gateway")

# Dev mode (localhost CORS + header logging) is chosen at deploy time:
#   MODEL_IN_MODAL_DEV=1 modal deploy -m backend.app
DEV = os.environ.get("MODEL_IN_MODAL_DEV") == "1"

IP_HEADER_HINTS = (
    "forwarded", "x-forwarded-for", "x-real-ip", "x-client-ip", "true-client-ip",
    "cf-connecting-ip", "fly-client-ip", "x-envoy-external-address",
)


# ---------------------------------------------------------------------------
# Settings (secrets come from Modal secrets as env vars)
# ---------------------------------------------------------------------------
@dataclass(frozen=True, repr=False)
class Settings:
    signing_key: bytes
    turnstile_secret: str
    vllm_api_key: str
    proxy_key: str
    proxy_secret: str
    vllm_url: str = config.VLLM_BASE_URL
    dev: bool = False
    loadtest_key: str = ""  # empty = load-test bypass disabled

    def __repr__(self) -> str:  # never let secrets reach a log line
        return f"Settings(vllm_url={self.vllm_url!r}, dev={self.dev}, secrets=<redacted>)"

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Settings:
        env = os.environ if env is None else env
        key = env["SESSION_SIGNING_KEY"]
        if len(key) < 32:
            raise RuntimeError("SESSION_SIGNING_KEY must be at least 32 characters")
        loadtest_key = env.get("LOADTEST_KEY", "")
        if len(loadtest_key) < config.LOADTEST_MIN_KEY_CHARS:
            loadtest_key = ""  # e.g. "off": bypass disabled
        return cls(
            signing_key=key.encode(),
            turnstile_secret=env["TURNSTILE_SECRET"],
            vllm_api_key=env["VLLM_API_KEY"],
            proxy_key=env["MODAL_PROXY_KEY"],
            proxy_secret=env["MODAL_PROXY_SECRET"],
            dev=DEV,
            loadtest_key=loadtest_key,
        )

    def is_loadtest(self, header_value: str | None) -> bool:
        """True only if the bypass is enabled and the header matches (constant time)."""
        if not self.loadtest_key or not header_value:
            return False
        return hmac.compare_digest(header_value.encode(), self.loadtest_key.encode())

    def upstream_headers(self) -> dict[str, str]:
        return {
            "Modal-Key": self.proxy_key,
            "Modal-Secret": self.proxy_secret,
            "Authorization": f"Bearer {self.vllm_api_key}",
        }


# ---------------------------------------------------------------------------
# Upstream: vLLM client + "is a GPU container warm?" tracking + wake-up ping
# ---------------------------------------------------------------------------
class Upstream:
    def __init__(
        self, client: httpx.AsyncClient, settings: Settings,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.client = client
        self.settings = settings
        self._clock = clock
        self._warm_until = float("-inf")
        self._wake_task: asyncio.Task | None = None

    def is_warm(self) -> bool:
        return self._clock() < self._warm_until

    def mark_warm(self) -> None:
        self._warm_until = self._clock() + config.WARM_ASSUME_S

    def mark_cold(self) -> None:
        self._warm_until = float("-inf")

    @property
    def waking(self) -> bool:
        return self._wake_task is not None and not self._wake_task.done()

    def wake(self) -> None:
        """Start one background wake-up loop unless vLLM is warm or already waking."""
        if self.is_warm() or self.waking:
            return
        self._wake_task = asyncio.get_running_loop().create_task(self._wake_loop())

    async def _wake_loop(self) -> None:
        started = self._clock()
        url = f"{self.settings.vllm_url}/health"
        # Wait past Modal's 150 s mark, so Modal (not us) ends each wait with a 303,
        # and follow that 303: it resumes the same queued request instead of
        # cancelling it and joining the GPU queue again from the back.
        timeout = httpx.Timeout(config.WAKE_PING_TIMEOUT_S, connect=config.UPSTREAM_CONNECT_TIMEOUT_S)
        attempt = 0
        while (remaining := config.WAKE_MAX_S - (self._clock() - started)) > 0:
            attempt += 1
            try:
                async with asyncio.timeout(remaining):
                    r = await self.client.get(url, headers=self.settings.upstream_headers(),
                                              timeout=timeout, follow_redirects=True)
            except (TimeoutError, httpx.TimeoutException, httpx.TooManyRedirects) as e:
                log.info("wake ping %d: %s at %.0fs", attempt, type(e).__name__, self._clock() - started)
                continue  # still queued for a GPU or booting: ask again
            except httpx.HTTPError as e:
                log.warning("wake ping failed: %s", type(e).__name__)
                await asyncio.sleep(config.WAKE_RETRY_PAUSE_S)
                continue
            if r.status_code == 200:
                self.mark_warm()
                log.info("upstream warm after %.0fs", self._clock() - started)
                return
            if r.status_code in (401, 403):
                log.error("wake ping rejected (%d): check proxy token / vLLM key", r.status_code)
                return
            if 300 <= r.status_code < 400:  # 303 = Modal still starting it after 150 s
                await asyncio.sleep(1)  # never spin if a redirect ever comes back instantly
            else:
                log.warning("wake ping status %d", r.status_code)
                await asyncio.sleep(config.WAKE_RETRY_PAUSE_S)
        log.warning("wake gave up after %.0fs", self._clock() - started)

    def chat_request(self, body: dict[str, Any]) -> httpx.Request:
        return self.client.build_request(
            "POST",
            f"{self.settings.vllm_url}/v1/chat/completions",
            json=body,
            headers=self.settings.upstream_headers(),
            timeout=httpx.Timeout(config.UPSTREAM_READ_TIMEOUT_S, connect=config.UPSTREAM_CONNECT_TIMEOUT_S),
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
class BodyError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def error_response(
    status: int, code: str, message: str,
    retry_after: int | None = None, resets_at: str | None = None,
) -> JSONResponse:
    body: dict[str, Any] = {"error": code, "message": message}
    headers = {}
    if retry_after is not None:
        body["retry_after"] = int(retry_after)
        headers["Retry-After"] = str(int(retry_after))
    if resets_at is not None:
        body["resets_at"] = resets_at
    return JSONResponse(body, status_code=status, headers=headers)


async def read_json(request: Request) -> Any:
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > config.MAX_BODY_BYTES:
        raise BodyError(413, "request_too_large", "Request body is too large.")
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > config.MAX_BODY_BYTES:
            raise BodyError(413, "request_too_large", "Request body is too large.")
        chunks.append(chunk)
    try:
        return json.loads(b"".join(chunks))
    except ValueError:
        raise BodyError(400, "invalid_request", "Body must be JSON.") from None


def client_ip(request: Request) -> str:
    if config.CLIENT_IP_HEADER:
        # Proxies append to forwarding headers; the right-most entry is the one
        # added by the proxy in front of us, the rest are client-controlled.
        value = request.headers.get(config.CLIENT_IP_HEADER, "").split(",")[-1].strip()
        if value:
            return value
    return request.client.host if request.client else "unknown"


def sse(event: dict[str, Any]) -> bytes:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode()


def log_line(kind: str, **fields: Any) -> None:
    log.info("%s %s", kind, " ".join(f"{k}={v}" for k, v in fields.items()))


class DevHeaderLog:
    """Dev only: log header *names*, plus values of IP-carrying headers, per request."""

    def __init__(self, app: Any):
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
            ips = {k: headers[k] for k in IP_HEADER_HINTS if k in headers}
            log.info("dev headers path=%s peer=%s names=%s ip_headers=%s",
                     scope.get("path"), scope.get("client"), sorted(headers), ips)
        await self.app(scope, receive, send)


# ---------------------------------------------------------------------------
# App factory (all dependencies injected, so tests can use fakes)
# ---------------------------------------------------------------------------
def create_app(
    *,
    settings: Settings,
    limiter: ratelimit.RateLimiter,
    spend: meter.SpendCache,
    upstream: Upstream,
    turnstile_client: httpx.AsyncClient,
    clock: Callable[[], float] = time.time,
) -> FastAPI:
    api = FastAPI(title="model-in-modal gateway", docs_url=None, redoc_url=None, openapi_url=None)
    api.add_middleware(
        CORSMiddleware,
        allow_origins=config.allowed_origins(),
        allow_origin_regex=config.DEV_ORIGIN_REGEX if settings.dev else None,
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type"],
        allow_credentials=False,
        max_age=600,
    )
    if settings.dev:
        api.add_middleware(DevHeaderLog)

    @api.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "http_error")
        return error_response(exc.status_code, code, str(exc.detail))

    @api.exception_handler(BodyError)
    async def body_error(request: Request, exc: BodyError) -> JSONResponse:
        return error_response(exc.status, exc.code, exc.message)

    @api.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> JSONResponse:
        log.error("unhandled %s on %s", type(exc).__name__, request.url.path)
        return error_response(500, "internal_error", "Something went wrong.")

    async def budget_problem() -> str | None:
        """None if the service may spend money, else why not. Fails closed."""
        try:
            spent = await spend.get()
        except Exception as e:
            log.error("spend meter read failed: %s", type(e).__name__)
            return "unavailable"
        return "credits_exhausted" if spent >= config.SOFT_CAP_USD else None

    def budget_response(problem: str) -> JSONResponse:
        if problem == "credits_exhausted":
            return error_response(503, "credits_exhausted",
                                  "This month's free credit is used up.", resets_at=meter.resets_at())
        return error_response(503, "unavailable", "The service is temporarily unavailable.",
                              retry_after=config.UNAVAILABLE_RETRY_AFTER_S)

    def warming_response() -> JSONResponse:
        return error_response(503, "warming_up", "Waking up the model. This can take a few minutes.",
                              retry_after=config.WARMING_RETRY_AFTER_S)

    # ------------------------------------------------------------------ status
    @api.get("/api/status")
    async def status() -> JSONResponse:
        problem = await budget_problem()
        return JSONResponse(
            {"available": problem is None, "reason": problem, "resets_at": meter.resets_at()},
            headers={"Cache-Control": "no-store"},
        )

    # ----------------------------------------------------------------- session
    @api.post("/api/session")
    async def create_session(request: Request) -> Any:
        ip = client_ip(request)
        body = await read_json(request)
        token = body.get("turnstile_token") if isinstance(body, dict) else None
        if not isinstance(token, str) or not 0 < len(token) <= config.MAX_TURNSTILE_TOKEN_CHARS:
            return error_response(400, "invalid_request", 'Body must be {"turnstile_token": "..."}.')

        problem = await budget_problem()
        if problem:
            log_line("session", ip=ip, status=503, result=problem)
            return budget_response(problem)

        try:
            result = await sessions.verify_turnstile(turnstile_client, settings.turnstile_secret, token, ip)
        except sessions.TurnstileUnavailable as e:
            log_line("session", ip=ip, status=502, result=f"turnstile_unavailable:{e}")
            return error_response(502, "turnstile_unavailable",
                                  "Could not verify the challenge. Try again.", retry_after=5)
        if not result.success:
            log_line("session", ip=ip, status=403, result="turnstile_failed",
                     codes=",".join(result.error_codes) or "-")
            return error_response(403, "turnstile_failed", "The challenge failed. Reload and try again.")

        s = sessions.issue(settings.signing_key, now=clock())
        upstream.wake()  # a new visitor will probably chat: start a GPU now
        log_line("session", sid=s.session_id, ip=ip, status=200, result="ok")
        return {"token": s.token, "expires_at": sessions.iso_utc(s.expires_at)}

    # -------------------------------------------------------------------- chat
    @api.post("/api/chat")
    async def chat(request: Request) -> Any:
        started = time.perf_counter()
        fields: dict[str, Any] = {"sid": "-", "ip": client_ip(request), "prompt_chars": 0}

        def finish(resp: JSONResponse, result: str) -> JSONResponse:
            log_line("chat", **fields, output_tokens=0, ttft_ms="-",
                     latency_ms=round((time.perf_counter() - started) * 1000),
                     status=resp.status_code, result=result)
            return resp

        # 1. session token
        sid = sessions.verify(sessions.bearer_token(request.headers.get("authorization")),
                              settings.signing_key, now=clock())
        if sid is None:
            return finish(error_response(401, "unauthorized", "Session expired or invalid."), "unauthorized")
        fields["sid"] = sid

        # 2. budget
        problem = await budget_problem()
        if problem:
            return finish(budget_response(problem), problem)

        # A wake-up is in progress and vLLM hasn't answered yet: say so without
        # touching Redis or the GPU. The page retries after retry_after.
        if upstream.waking and not upstream.is_warm():
            return finish(warming_response(), "warming_up")

        # 3 + 4. per-session / per-IP limits, then the global stream cap
        loadtest = settings.is_loadtest(request.headers.get(config.LOADTEST_HEADER))
        if loadtest:
            fields["loadtest"] = 1
        try:
            decision, ticket = await limiter.admit(sid, fields["ip"], skip_visitor_limits=loadtest)
        except ratelimit.RateLimitUnavailable as e:
            log.error("rate limiter unavailable: %s", e)
            return finish(error_response(503, "unavailable", "The service is temporarily unavailable.",
                                         retry_after=config.UNAVAILABLE_RETRY_AFTER_S), "redis_unavailable")
        if not decision.allowed:
            return finish(error_response(decision.status, decision.error, decision.message,
                                         retry_after=decision.retry_after), decision.error)

        handed_off, refund = False, False
        try:
            # 5. shape
            try:
                body, fields["prompt_chars"] = shaping.shape(await read_json(request))
            except (BodyError, shaping.ShapingError) as e:
                return finish(error_response(e.status, e.code, e.message), e.code)

            # 6. forward
            try:
                resp = await asyncio.wait_for(
                    upstream.client.send(upstream.chat_request(body), stream=True),
                    config.UPSTREAM_WARM_WAIT_S,
                )
            except (TimeoutError, httpx.TimeoutException):
                refund = True
                upstream.mark_cold()
                upstream.wake()
                return finish(warming_response(), "warming_up")
            except httpx.HTTPError as e:
                refund = True
                log.warning("upstream request failed: %s", type(e).__name__)
                return finish(error_response(502, "upstream_error", "The model could not be reached. Try again."),
                              "upstream_error")

            if resp.status_code != 200:
                await resp.aclose()
                refund = True
                if 300 <= resp.status_code < 400:
                    upstream.mark_cold()
                    upstream.wake()
                    return finish(warming_response(), "warming_up")
                log.warning("upstream status %d", resp.status_code)
                return finish(error_response(502, "upstream_error", "The model returned an error. Try again."),
                              f"upstream_{resp.status_code}")

            upstream.mark_warm()
            handed_off = True
            return StreamingResponse(
                relay(resp, ticket, fields, started),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )
        finally:
            if not handed_off:
                with anyio.CancelScope(shield=True):
                    await limiter.release(ticket, refund=refund)

    async def relay(
        resp: httpx.Response, ticket: ratelimit.Ticket, fields: dict[str, Any], started: float
    ) -> AsyncIterator[bytes]:
        """Turn vLLM's OpenAI-style SSE into the gateway's small event format.

        The stream slot is released *before* the final event goes out, so a
        visitor who replies instantly never hits a false "one at a time" 429.
        """
        result, output_tokens, ttft_ms, finish_reason, sent_text = "ok", 0, None, None, False
        released = False

        async def release() -> None:
            nonlocal released
            if released:
                return
            released = True
            with anyio.CancelScope(shield=True):
                try:
                    await resp.aclose()  # also stops generation on the GPU if the visitor left
                except Exception as e:
                    log.warning("closing upstream stream failed: %s", type(e).__name__)
                await limiter.release(ticket, refund=not sent_text and result != "ok")  # never raises

        deadline = time.perf_counter() + config.STREAM_MAX_DURATION_S
        final: dict[str, Any] | None = None
        pending: list[str] = []  # token text received but not yet sent
        last_flush = 0.0
        try:
            try:
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except ValueError:
                        continue
                    if not isinstance(chunk, dict):
                        continue
                    if "error" in chunk:  # vLLM reports mid-stream failures as a data object
                        result = "upstream_error"
                        final = {"type": "error", "error": "upstream_error",
                                 "message": "The model stopped with an error."}
                        break
                    usage = chunk.get("usage")
                    if isinstance(usage, dict) and usage.get("completion_tokens") is not None:
                        output_tokens = int(usage["completion_tokens"])
                    for choice in chunk.get("choices") or []:
                        text = (choice.get("delta") or {}).get("content")
                        if text:
                            if ttft_ms is None:
                                ttft_ms = round((time.perf_counter() - started) * 1000)
                            pending.append(text)
                        finish_reason = choice.get("finish_reason") or finish_reason
                    now = time.perf_counter()
                    if pending and (not sent_text or now - last_flush >= config.STREAM_FLUSH_INTERVAL_S):
                        yield sse({"type": "delta", "text": "".join(pending)})
                        pending.clear()
                        sent_text, last_flush = True, now
                    if now > deadline:
                        result = "timeout"
                        final = {"type": "error", "error": "timeout",
                                 "message": "The response took too long and was cut off."}
                        break
            except httpx.HTTPError as e:
                result = f"upstream_{type(e).__name__}"
                final = {"type": "error", "error": "upstream_error",
                         "message": "The connection to the model dropped."}
            if pending:  # whatever arrived since the last batch
                yield sse({"type": "delta", "text": "".join(pending)})
                pending.clear()
                sent_text = True
            if final is None:
                final = {"type": "done", "finish_reason": finish_reason,
                         "output_tokens": output_tokens, "ttft_ms": ttft_ms}
            await release()  # free the slot first, then tell the browser we're finished
            yield sse(final)
        except (asyncio.CancelledError, GeneratorExit):
            result = "client_disconnected"
            raise
        finally:
            await release()
            if result == "ok":
                upstream.mark_warm()
            log_line("chat", **fields, output_tokens=output_tokens, ttft_ms=ttft_ms if ttft_ms is not None else "-",
                     latency_ms=round((time.perf_counter() - started) * 1000), status=200, result=result)

    return api


# ---------------------------------------------------------------------------
# Modal deployment
# ---------------------------------------------------------------------------
app = modal.App(f"{config.APP_NAME}-gateway")

gateway_image = (
    modal.Image.debian_slim(python_version=config.PYTHON_VERSION)
    .uv_pip_install(
        f"fastapi=={config.FASTAPI_VERSION}",
        f"httpx=={config.HTTPX_VERSION}",
        f"upstash-redis=={config.UPSTASH_REDIS_VERSION}",
    )
    .env({"MODEL_IN_MODAL_DEV": "1" if DEV else "0"})
)


def build_app() -> FastAPI:
    """Production wiring: real Upstash, Modal Dict spend meter, vLLM over HTTPS."""
    from upstash_redis.asyncio import Redis

    settings = Settings.from_env()
    redis = Redis(
        os.environ["UPSTASH_REDIS_REST_URL"],
        os.environ["UPSTASH_REDIS_REST_TOKEN"],
        rest_retries=1,
        rest_retry_interval=0.5,
        allow_telemetry=False,
    )
    upstream_client = httpx.AsyncClient(
        follow_redirects=False,
        limits=httpx.Limits(max_connections=config.GLOBAL_MAX_STREAMS + 10, max_keepalive_connections=20),
    )
    return create_app(
        settings=settings,
        limiter=ratelimit.RateLimiter(redis, ip_key=settings.signing_key),
        spend=meter.SpendCache(),
        upstream=Upstream(upstream_client, settings),
        turnstile_client=httpx.AsyncClient(follow_redirects=False),
    )


@app.cls(
    image=gateway_image,
    cpu=config.GATEWAY_CPU_CORES,
    memory=config.GATEWAY_MEMORY_MIB,
    min_containers=config.GATEWAY_MIN_CONTAINERS,
    max_containers=config.GATEWAY_MAX_CONTAINERS,
    scaledown_window=config.GATEWAY_SCALEDOWN_WINDOW_S,
    timeout=config.GATEWAY_REQUEST_TIMEOUT_S,
    secrets=[
        modal.Secret.from_name(
            config.SECRET_GATEWAY,
            required_keys=["SESSION_SIGNING_KEY", "TURNSTILE_SECRET",
                           "UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN"],
        ),
        modal.Secret.from_name(config.SECRET_PROXY, required_keys=["MODAL_PROXY_KEY", "MODAL_PROXY_SECRET"]),
        modal.Secret.from_name(config.SECRET_VLLM, required_keys=["VLLM_API_KEY"]),
        modal.Secret.from_name(config.SECRET_LOADTEST, required_keys=["LOADTEST_KEY"]),
    ],
)
@modal.concurrent(max_inputs=config.GATEWAY_MAX_INPUTS, target_inputs=config.GATEWAY_TARGET_INPUTS)
class Gateway:
    @modal.enter()
    def start(self):
        self.meter = meter.SpendMeter(kind="gateway", rate=meter.GATEWAY_RATE).start()
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
        logging.getLogger("httpx").setLevel(logging.WARNING)  # no per-request URL lines
        log.info("gateway container up: region=%s cloud=%s", os.environ.get("MODAL_REGION", "unknown"),
                 os.environ.get("MODAL_CLOUD_PROVIDER", "unknown"))

    @modal.asgi_app(label=config.GATEWAY_LABEL)
    def web(self) -> FastAPI:
        return build_app()

    @modal.exit()
    def stop(self):
        self.meter.stop()
