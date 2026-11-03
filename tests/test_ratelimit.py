import asyncio

import pytest
from fakes import Clock, FakeRedis

from backend import config, ratelimit

KEY = b"k" * 32
# 2026-11-20 10:00:30 UTC: 30 s into a minute, 14 h before midnight.
T0 = 1_795_168_830.0


def make(t=T0):
    clock = Clock(t)
    redis = FakeRedis(clock)
    return ratelimit.RateLimiter(redis, ip_key=KEY, clock=clock), redis, clock


def run(coro):
    return asyncio.run(coro)


def admit_and_release(limiter, sid, ip="203.0.113.7"):
    decision, ticket = run(limiter.admit(sid, ip))
    if ticket:
        run(limiter.release(ticket))
    return decision


# ---------------------------------------------------------------- message limits
def test_per_minute_limit_and_retry_after():
    limiter, _, clock = make()
    for _ in range(config.SESSION_MSGS_PER_MIN):
        assert admit_and_release(limiter, "s1").allowed
    d = admit_and_release(limiter, "s1")
    assert (d.allowed, d.status, d.error) == (False, 429, "rate_limited")
    assert d.retry_after == 30  # until the next minute starts
    assert "minute" in d.message

    clock.t += 30  # next minute
    assert admit_and_release(limiter, "s1").allowed


def test_per_day_limit_per_session():
    limiter, _, clock = make()
    for i in range(config.SESSION_MSGS_PER_DAY):
        if i and i % config.SESSION_MSGS_PER_MIN == 0:
            clock.t += 60
        assert admit_and_release(limiter, "s1").allowed
    clock.t += 60
    d = admit_and_release(limiter, "s1")
    assert d.status == 429 and "Daily" in d.message and "session" in d.message
    midnight = (int(clock.t) // 86400 + 1) * 86400
    assert d.retry_after == pytest.approx(midnight - clock.t, abs=1)
    # A different session on the same IP is still fine.
    assert admit_and_release(limiter, "s2").allowed


def test_per_ip_limit_across_sessions():
    limiter, _, clock = make()
    for i in range(config.IP_MSGS_PER_DAY):
        assert admit_and_release(limiter, f"s{i}", ip="198.51.100.1").allowed
    d = admit_and_release(limiter, "fresh", ip="198.51.100.1")
    assert d.status == 429 and "network" in d.message
    assert admit_and_release(limiter, "fresh", ip="198.51.100.2").allowed


def test_new_day_resets_daily_limits():
    limiter, _, clock = make()
    for i in range(config.IP_MSGS_PER_DAY):
        admit_and_release(limiter, f"s{i}", ip="198.51.100.1")
    assert not admit_and_release(limiter, "x", ip="198.51.100.1").allowed
    clock.t = (int(clock.t) // 86400 + 1) * 86400 + 1
    assert admit_and_release(limiter, "x", ip="198.51.100.1").allowed


def test_blocked_visitor_does_not_spend_redis_commands():
    limiter, redis, _ = make()
    for _ in range(config.SESSION_MSGS_PER_MIN + 1):
        admit_and_release(limiter, "s1")
    before = redis.commands
    for _ in range(20):
        d = admit_and_release(limiter, "s1")
        assert d.status == 429 and 1 <= d.retry_after <= 30
    assert redis.commands == before


# ---------------------------------------------------------------- stream slots
def test_one_stream_per_session():
    limiter, _, _ = make()
    first, ticket = run(limiter.admit("s1", "203.0.113.7"))
    assert first.allowed
    second = admit_and_release(limiter, "s1")
    assert (second.status, second.retry_after) == (429, config.STREAM_IN_PROGRESS_RETRY_AFTER_S)
    assert "current response" in second.message
    assert admit_and_release(limiter, "s2").allowed  # other sessions unaffected
    run(limiter.release(ticket))
    assert admit_and_release(limiter, "s1").allowed


def test_crashed_stream_slot_expires():
    limiter, _, clock = make()
    _, ticket = run(limiter.admit("s1", "203.0.113.7"))  # never released
    assert admit_and_release(limiter, "s1").status == 429
    clock.t += config.STREAM_SLOT_TTL_S + 1
    assert admit_and_release(limiter, "s1").allowed


def test_global_cap_returns_busy_and_refunds():
    limiter, redis, _ = make()
    tickets = []
    for i in range(config.GLOBAL_MAX_STREAMS):
        d, t = run(limiter.admit(f"s{i}", f"10.0.{i // 250}.{i % 250}"))
        assert d.allowed
        tickets.append(t)
    d, t = run(limiter.admit("late", "192.0.2.1"))
    assert (d.allowed, d.status, d.error, d.retry_after) == (False, 503, "busy", config.BUSY_RETRY_AFTER_S)
    assert t is None
    # The rejected request was not counted against the visitor.
    assert all(redis.get(k) in (0, None) for k in redis.keys() if ":s:late:" in k)
    run(limiter.release(tickets[0]))
    assert admit_and_release(limiter, "late", "192.0.2.1").allowed


def test_release_with_refund_uncounts_message():
    limiter, redis, _ = make()
    d, ticket = run(limiter.admit("s1", "203.0.113.7"))
    run(limiter.release(ticket, refund=True))
    for key, _ in ticket.counters:
        assert redis.get(key) == 0
    assert redis.zsets == {}


# ---------------------------------------------------------------- storage hygiene
def test_every_key_has_a_ttl_and_ip_is_hashed():
    limiter, redis, _ = make()
    d, ticket = run(limiter.admit("s1", "203.0.113.7"))
    keys = redis.keys()
    assert keys and all(redis.ttl(k) > 0 for k in keys)
    assert not any("203.0.113.7" in k for k in keys)
    run(limiter.release(ticket, refund=True))
    assert all(redis.ttl(k) > 0 for k in redis.keys())


def test_refund_after_window_rollover_leaves_no_permanent_key():
    limiter, redis, clock = make()
    _, ticket = run(limiter.admit("s1", "203.0.113.7"))
    clock.t += 3 * 86400  # every counter has expired
    run(limiter.release(ticket, refund=True))
    assert all(redis.ttl(k) > 0 for k in redis.keys())


# ---------------------------------------------------------------- failures
def test_redis_down_raises_unavailable():
    limiter, redis, _ = make()
    redis.fail = True
    with pytest.raises(ratelimit.RateLimitUnavailable):
        run(limiter.admit("s1", "203.0.113.7"))


def test_redis_hang_times_out(monkeypatch):
    monkeypatch.setattr(config, "REDIS_TIMEOUT_S", 0.05)
    limiter, redis, _ = make()
    redis.hang = True
    with pytest.raises(ratelimit.RateLimitUnavailable):
        run(limiter.admit("s1", "203.0.113.7"))


def test_release_never_raises():
    limiter, redis, _ = make()
    _, ticket = run(limiter.admit("s1", "203.0.113.7"))
    redis.fail = True
    run(limiter.release(ticket, refund=True))  # logs, does not raise
