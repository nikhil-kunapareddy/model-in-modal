"""Per-session / per-IP message limits and stream slots in Upstash Redis.

Admission is one MULTI/EXEC transaction (atomic), then Python decides in the
order the gateway requires: message limits (429), one stream per session (429),
global stream cap (503 busy).

Message counters use fixed windows; the window is part of the key, and each
counter is INCR + EXPIRE in the transaction:

    rl:s:{session}:m:{unix_minute}   SESSION_MSGS_PER_MIN
    rl:s:{session}:d:{yyyymmdd}      SESSION_MSGS_PER_DAY   (UTC day)
    rl:ip:{ip_hash}:d:{yyyymmdd}     IP_MSGS_PER_DAY        (UTC day)

A message counts once admitted. It is refunded only when the service (not the
visitor) fails to answer: model warming up, upstream error, or global cap.

Stream slots are sorted sets of {ticket: start_time}:

    rl:s:{session}:streams   at most SESSION_MAX_CONCURRENT_STREAMS
    rl:streams               at most GLOBAL_MAX_STREAMS

Members older than STREAM_SLOT_TTL_S are dropped on every admission, so a
crashed stream can't hold a slot, or lock a session, for longer than that.

The load-test bypass (skip_visitor_limits) skips the counters and the
per-session slot; it still takes a global slot, so the global cap holds.

IPs are stored only as a keyed hash. After a 429, the session or IP is also
blocked in this container's memory until its window ends, so repeated attempts
don't spend Upstash commands.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import math
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from backend import config

log = logging.getLogger("model-in-modal.ratelimit")

MINUTE = 60
DAY = 24 * 60 * 60
GLOBAL_STREAMS_KEY = "rl:streams"
_LOCAL_BLOCKS_MAX = 10_000  # memory guard for the in-container block list


class RateLimitUnavailable(Exception):
    """Upstash could not be reached in time. The gateway fails closed."""


@dataclass(frozen=True)
class Decision:
    allowed: bool
    status: int = 200
    error: str | None = None
    message: str = ""
    retry_after: int = 0


@dataclass(frozen=True)
class Ticket:
    """What an admitted request holds: counters (to refund) and slots (to release)."""

    counters: tuple[tuple[str, int], ...]  # (key, ttl)
    slots: tuple[tuple[str, str], ...]  # (key, member)


@dataclass(frozen=True)
class _Counter:
    key: str
    limit: int
    ttl: int
    retry_after: float
    scope: str  # in-memory block key
    message: str


def _seconds(x: float) -> int:
    return max(1, math.ceil(x))


def _limited(message: str, retry_after: float) -> Decision:
    return Decision(False, 429, "rate_limited", message, _seconds(retry_after))


class RateLimiter:
    def __init__(self, redis: Any, ip_key: bytes, clock: Callable[[], float] = time.time):
        self._redis = redis  # upstash_redis.asyncio.Redis (or a test fake)
        self._ip_key = ip_key
        self._clock = clock
        self._blocked: dict[str, tuple[float, str]] = {}

    def ip_hash(self, ip: str) -> str:
        return hmac.new(self._ip_key, ip.encode(), hashlib.sha256).hexdigest()[:20]

    def _counters(self, session_id: str, ip_hash: str, now: float) -> list[_Counter]:
        minute = int(now // MINUTE)
        day = time.strftime("%Y%m%d", time.gmtime(now))
        to_next_minute = MINUTE - now % MINUTE
        to_midnight = DAY - now % DAY
        session, ip = f"s:{session_id}", f"ip:{ip_hash}"
        return [
            _Counter(f"rl:{session}:m:{minute}", config.SESSION_MSGS_PER_MIN, 2 * MINUTE,
                     to_next_minute, session, "Too many messages this minute."),
            _Counter(f"rl:{session}:d:{day}", config.SESSION_MSGS_PER_DAY, 2 * DAY,
                     to_midnight, session, "Daily message limit reached for this session."),
            _Counter(f"rl:{ip}:d:{day}", config.IP_MSGS_PER_DAY, 2 * DAY,
                     to_midnight, ip, "Daily message limit reached for your network."),
        ]

    async def admit(
        self, session_id: str, ip: str, *, skip_visitor_limits: bool = False
    ) -> tuple[Decision, Ticket | None]:
        """Count one message and take one stream slot, or say why not.

        `skip_visitor_limits` (load test only) skips the per-session and per-IP
        limits. The global stream cap always applies.

        Raises RateLimitUnavailable if Upstash can't be reached.
        """
        now = self._clock()
        ip_hash = self.ip_hash(ip)
        slots = [(GLOBAL_STREAMS_KEY, config.GLOBAL_MAX_STREAMS)]
        counters: list[_Counter] = []
        if not skip_visitor_limits:
            for scope in (f"s:{session_id}", f"ip:{ip_hash}"):
                blocked = self._blocked.get(scope)
                if blocked and blocked[0] > now:
                    return _limited(blocked[1], blocked[0] - now), None
            counters = self._counters(session_id, ip_hash, now)
            slots.insert(0, (f"rl:s:{session_id}:streams", config.SESSION_MAX_CONCURRENT_STREAMS))
        member = uuid.uuid4().hex

        tx = self._redis.multi()
        for c in counters:
            tx.incr(c.key)
            tx.expire(c.key, c.ttl)
        for key, _ in slots:
            tx.zremrangebyscore(key, "-inf", now - config.STREAM_SLOT_TTL_S)
            tx.zadd(key, {member: now})
            tx.zcard(key)
            tx.expire(key, config.STREAM_SLOT_TTL_S)
        results = await self._run(tx.exec())

        n = len(counters)
        counts = [int(results[2 * i]) for i in range(n)]
        in_use = [int(results[2 * n + 4 * i + 2]) for i in range(len(slots))]
        ticket = Ticket(
            counters=tuple((c.key, c.ttl) for c in counters),
            slots=tuple((key, member) for key, _ in slots),
        )

        for c, count in zip(counters, counts):
            if count > c.limit:
                await self.release(ticket)
                self._block(c.scope, now + c.retry_after, c.message)
                return _limited(c.message, c.retry_after), None
        for (key, limit), used in zip(slots, in_use):
            if used > limit and key != GLOBAL_STREAMS_KEY:  # this session's own stream slot
                await self.release(ticket)
                return _limited("Wait for the current response to finish.",
                                config.STREAM_IN_PROGRESS_RETRY_AFTER_S), None
        if in_use[-1] > config.GLOBAL_MAX_STREAMS:  # the global slot is always last
            await self.release(ticket, refund=True)
            return Decision(False, 503, "busy", "The model is busy. Try again shortly.",
                            config.BUSY_RETRY_AFTER_S), None
        return Decision(True), ticket

    async def release(self, ticket: Ticket, refund: bool = False) -> None:
        """Free the ticket's stream slots and, if `refund`, uncount its message. Never raises."""
        p = self._redis.pipeline()
        for key, member in ticket.slots:
            p.zrem(key, member)
        if refund:
            for key, ttl in ticket.counters:
                p.decr(key)
                p.expire(key, ttl)  # if the window already rolled over, don't leave a stray key
        try:
            await self._run(p.exec())
        except RateLimitUnavailable as e:
            log.warning("release failed (%s); slots expire after %ss", e, config.STREAM_SLOT_TTL_S)

    async def _run(self, call: Awaitable[Any]) -> Any:
        try:
            async with asyncio.timeout(config.REDIS_TIMEOUT_S):
                return await call
        except Exception as e:
            raise RateLimitUnavailable(type(e).__name__) from None

    def _block(self, scope: str, until: float, message: str) -> None:
        if len(self._blocked) >= _LOCAL_BLOCKS_MAX:
            now = self._clock()
            self._blocked = {k: v for k, v in self._blocked.items() if v[0] > now}
        current = self._blocked.get(scope)
        if current is None or current[0] < until:
            self._blocked[scope] = (until, message)
