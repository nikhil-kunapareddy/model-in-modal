"""Session tokens and Cloudflare Turnstile verification.

No cookies: the browser keeps the token in sessionStorage and sends it as
`Authorization: Bearer <token>`. Token format:

    v1.<session_id>.<expires_unix>.<signature>

    signature = base64url(HMAC-SHA256(SESSION_SIGNING_KEY, "v1.<session_id>.<expires_unix>"))
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass

import httpx

from backend import config

VERSION = "v1"
MAX_TOKEN_CHARS = 256


@dataclass(frozen=True)
class Session:
    session_id: str
    expires_at: int  # unix seconds
    token: str


def _sign(key: bytes, payload: str) -> str:
    mac = hmac.new(key, payload.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).rstrip(b"=").decode()


def issue(key: bytes, now: float | None = None, ttl: int = config.SESSION_TTL_S) -> Session:
    """A new random session id with a signed token that expires `ttl` seconds from now."""
    now = time.time() if now is None else now
    session_id = secrets.token_urlsafe(16)  # 128 bits; alphabet has no "."
    expires_at = int(now) + ttl
    payload = f"{VERSION}.{session_id}.{expires_at}"
    return Session(session_id, expires_at, f"{payload}.{_sign(key, payload)}")


def verify(token: str | None, key: bytes, now: float | None = None) -> str | None:
    """The session id if `token` is authentic and unexpired, else None. Never raises."""
    if not token or len(token) > MAX_TOKEN_CHARS:
        return None
    parts = token.split(".")
    if len(parts) != 4 or parts[0] != VERSION:
        return None
    _, session_id, expires, signature = parts
    if not session_id or not expires.isdigit():
        return None
    expected = _sign(key, f"{VERSION}.{session_id}.{expires}")
    if not hmac.compare_digest(signature.encode(), expected.encode()):
        return None
    now = time.time() if now is None else now
    if int(expires) <= now:
        return None
    return session_id


def bearer_token(header: str | None) -> str | None:
    """The token from an `Authorization: Bearer <token>` header, if present."""
    if not header:
        return None
    scheme, _, value = header.partition(" ")
    value = value.strip()
    return value if scheme.lower() == "bearer" and value else None


def iso_utc(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


# ---------------------------------------------------------------------------
# Turnstile
# ---------------------------------------------------------------------------
class TurnstileUnavailable(Exception):
    """Cloudflare's siteverify endpoint could not be reached or answered oddly."""


@dataclass(frozen=True)
class TurnstileResult:
    success: bool
    error_codes: tuple[str, ...] = ()


async def verify_turnstile(
    client: httpx.AsyncClient, secret: str, token: str, remoteip: str | None = None
) -> TurnstileResult:
    """Validate a widget token with Cloudflare's siteverify API (single use, 300 s)."""
    data = {"secret": secret, "response": token}
    if remoteip:
        data["remoteip"] = remoteip
    try:
        r = await client.post(
            config.TURNSTILE_VERIFY_URL, data=data, timeout=config.TURNSTILE_TIMEOUT_S
        )
        r.raise_for_status()
        body = r.json()
    except (httpx.HTTPError, ValueError) as e:
        # `from None`: never chain an exception that carries the request (and secret).
        raise TurnstileUnavailable(type(e).__name__) from None
    if not isinstance(body, dict):
        raise TurnstileUnavailable("unexpected response")
    codes = body.get("error-codes") or ()
    return TurnstileResult(body.get("success") is True, tuple(str(c) for c in codes))
