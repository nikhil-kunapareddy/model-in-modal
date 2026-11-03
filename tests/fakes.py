"""In-memory stand-ins used by the tests."""

import asyncio
import time


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class FakeRedis:
    """Mimics the parts of upstash_redis.asyncio.Redis that ratelimit.py uses.

    Commands are queued on multi()/pipeline() and run on `await exec()`, like the
    real client. Expiry follows `clock`. Empty sorted sets disappear, as in Redis.
    """

    def __init__(self, clock=time.time):
        self.clock = clock
        self.strings: dict[str, int] = {}
        self.zsets: dict[str, dict[str, float]] = {}
        self.expiry: dict[str, float] = {}
        self.commands = 0
        self.fail = False
        self.hang = False

    # ------------------------------------------------------------ helpers
    def _purge(self, key):
        exp = self.expiry.get(key)
        if exp is not None and exp <= self.clock():
            self.strings.pop(key, None)
            self.zsets.pop(key, None)
            self.expiry.pop(key, None)

    def _drop_if_empty(self, key):
        if key in self.zsets and not self.zsets[key]:
            del self.zsets[key]
            self.expiry.pop(key, None)

    def keys(self):
        for k in list(self.strings) + list(self.zsets):
            self._purge(k)
        return set(self.strings) | set(self.zsets)

    def ttl(self, key):
        self._purge(key)
        if key not in self.strings and key not in self.zsets:
            return -2
        exp = self.expiry.get(key)
        return -1 if exp is None else exp - self.clock()

    def get(self, key):
        self._purge(key)
        return self.strings.get(key)

    # ----------------------------------------------------------- commands
    def incr(self, key):
        self._purge(key)
        self.strings[key] = int(self.strings.get(key, 0)) + 1
        return self.strings[key]

    def decr(self, key):
        self._purge(key)
        self.strings[key] = int(self.strings.get(key, 0)) - 1
        return self.strings[key]

    def expire(self, key, seconds):
        self._purge(key)
        if key not in self.strings and key not in self.zsets:
            return 0
        self.expiry[key] = self.clock() + seconds
        return 1

    def zadd(self, key, scores):
        self._purge(key)
        z = self.zsets.setdefault(key, {})
        added = sum(1 for m in scores if m not in z)
        z.update(scores)
        return added

    def zremrangebyscore(self, key, lo, hi):
        self._purge(key)
        z = self.zsets.get(key, {})
        lo, hi = float(lo), float(hi)
        doomed = [m for m, s in z.items() if lo <= s <= hi]
        for m in doomed:
            del z[m]
        self._drop_if_empty(key)
        return len(doomed)

    def zcard(self, key):
        self._purge(key)
        return len(self.zsets.get(key, {}))

    def zrem(self, key, *members):
        self._purge(key)
        z = self.zsets.get(key, {})
        removed = sum(1 for m in members if z.pop(m, None) is not None)
        self._drop_if_empty(key)
        return removed

    def multi(self):
        return FakePipeline(self)

    pipeline = multi


class FakePipeline:
    def __init__(self, redis):
        self.redis = redis
        self.stack = []

    def __getattr__(self, name):
        def queue(*args, **kwargs):
            self.stack.append((name, args, kwargs))
            return self

        return queue

    async def exec(self):
        if self.redis.hang:
            await asyncio.sleep(3600)
        if self.redis.fail:
            raise ConnectionError("redis down")
        out = []
        for name, args, kwargs in self.stack:
            self.redis.commands += 1
            out.append(getattr(self.redis, name)(*args, **kwargs))
        self.stack = []
        return out
