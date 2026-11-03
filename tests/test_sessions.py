import asyncio
import urllib.parse

import httpx
import pytest

from backend import config, sessions

KEY = b"k" * 32
NOW = 1_800_000_000.0


# ---------------------------------------------------------------- tokens
def test_issue_and_verify_roundtrip():
    s = sessions.issue(KEY, now=NOW)
    assert s.expires_at == int(NOW) + config.SESSION_TTL_S == int(NOW) + 86400
    assert s.token.startswith("v1.") and len(s.token.split(".")) == 4
    assert sessions.verify(s.token, KEY, now=NOW + 10) == s.session_id


def test_session_ids_are_random():
    ids = {sessions.issue(KEY, now=NOW).session_id for _ in range(200)}
    assert len(ids) == 200


def test_expiry_boundary():
    s = sessions.issue(KEY, now=NOW)
    assert sessions.verify(s.token, KEY, now=s.expires_at - 1) == s.session_id
    assert sessions.verify(s.token, KEY, now=s.expires_at) is None
    assert sessions.verify(s.token, KEY, now=s.expires_at + 3600) is None


def test_wrong_key_rejected():
    s = sessions.issue(KEY, now=NOW)
    assert sessions.verify(s.token, b"x" * 32, now=NOW) is None


@pytest.mark.parametrize("part", [1, 2, 3])
def test_tampering_rejected(part):
    s = sessions.issue(KEY, now=NOW)
    parts = s.token.split(".")
    if part == 2:  # push expiry out by a year
        parts[2] = str(int(parts[2]) + 365 * 86400)
    else:
        parts[part] = ("A" if parts[part][0] != "A" else "B") + parts[part][1:]
    assert sessions.verify(".".join(parts), KEY, now=NOW) is None


@pytest.mark.parametrize(
    "token",
    [None, "", "abc", "a.b.c", "v2.sid.1900000000.sig", "v1..1900000000.sig",
     "v1.sid.notanumber.sig", "v1.sid.1900000000.sïg", "v1.sid.1900000000.sig.extra", "v1." + "a" * 300],
)
def test_malformed_tokens_rejected_without_raising(token):
    assert sessions.verify(token, KEY, now=NOW) is None


@pytest.mark.parametrize(
    "header, expected",
    [("Bearer abc", "abc"), ("bearer abc", "abc"), ("Bearer  abc ", "abc"),
     ("Basic abc", None), ("Bearer", None), ("Bearer ", None), (None, None), ("", None)],
)
def test_bearer_token(header, expected):
    assert sessions.bearer_token(header) == expected


def test_iso_utc():
    assert sessions.iso_utc(0) == "1970-01-01T00:00:00Z"


# ---------------------------------------------------------------- Turnstile
def turnstile_client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_turnstile_success_sends_secret_token_and_ip():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["form"] = dict(urllib.parse.parse_qsl(request.content.decode()))
        return httpx.Response(200, json={"success": True, "hostname": "localhost"})

    result = asyncio.run(sessions.verify_turnstile(turnstile_client(handler), "sec", "tok", "203.0.113.7"))
    assert result.success
    assert seen["url"] == config.TURNSTILE_VERIFY_URL
    assert seen["form"] == {"secret": "sec", "response": "tok", "remoteip": "203.0.113.7"}


def test_turnstile_failure_returns_error_codes():
    def handler(request):
        return httpx.Response(200, json={"success": False, "error-codes": ["timeout-or-duplicate"]})

    result = asyncio.run(sessions.verify_turnstile(turnstile_client(handler), "sec", "tok"))
    assert not result.success
    assert result.error_codes == ("timeout-or-duplicate",)


@pytest.mark.parametrize(
    "handler",
    [
        lambda r: httpx.Response(500, text="oops"),
        lambda r: httpx.Response(200, text="not json"),
        lambda r: httpx.Response(200, json=["not", "a", "dict"]),
        lambda r: (_ for _ in ()).throw(httpx.ConnectError("down")),
    ],
)
def test_turnstile_unavailable(handler):
    with pytest.raises(sessions.TurnstileUnavailable) as e:
        asyncio.run(sessions.verify_turnstile(turnstile_client(handler), "sec", "tok"))
    assert "sec" not in str(e.value)


def test_turnstile_success_must_be_true_not_truthy():
    def handler(request):
        return httpx.Response(200, json={"success": "yes"})

    assert not asyncio.run(sessions.verify_turnstile(turnstile_client(handler), "s", "t")).success
