"""Spend meter: every container records its own running cost in a Modal Dict.

Layout of Dict `model-in-modal-spend`:
    "{cycle_id}:{container_id}" -> {"usd", "kind", "started_at", "updated_at"}
        one key per container, overwritten every METER_INTERVAL_S with the
        container's cumulative cost, plus one final write on exit.
    "{cycle_id}:closed" -> {"by_id": {container_id: usd}, "updated_at"}
        written only by the daily compaction job (see below).

cycle_id is the ISO date the current billing cycle started (e.g. "2026-11-03").

Modal Dict entries expire after 7 days without reads or writes, so a daily cron
job folds containers that stopped writing into the "closed" key, deletes their
own keys, and rewrites "closed" (resetting its timer). Containers report a
monotonically increasing cumulative cost, so totals take max(closed, live) per
container id: compaction can never double-count, even if it races a write.

total_spend(cycle) = Volume storage charge + sum over containers.
"""

from __future__ import annotations

import calendar
import datetime as dt
import functools
import logging
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Mapping
from typing import Any

import modal

from backend import config

log = logging.getLogger("model-in-modal.meter")

CLOSED = "closed"

app = modal.App(f"{config.APP_NAME}-meter")


# ---------------------------------------------------------------------------
# Rates
# ---------------------------------------------------------------------------
def container_rate(*, gpu_count: int, gpu_price_per_s: float, cpu_cores: float, memory_mib: int) -> float:
    """USD per second for one container with this resource request."""
    return (
        gpu_count * gpu_price_per_s
        + cpu_cores * config.PRICE_CPU_CORE_PER_S
        + (memory_mib / 1024) * config.PRICE_MEMORY_GIB_PER_S
    )


# Computed from the same constants the @app.cls / @app.function decorators use.
GPU_RATE = container_rate(
    gpu_count=config.GPU_COUNT,
    gpu_price_per_s=config.PRICE_L4_PER_S,
    cpu_cores=config.GPU_CPU_CORES,
    memory_mib=config.GPU_MEMORY_MIB,
)
GATEWAY_RATE = container_rate(
    gpu_count=0,
    gpu_price_per_s=0.0,
    cpu_cores=config.GATEWAY_CPU_CORES,
    memory_mib=config.GATEWAY_MEMORY_MIB,
)


def storage_charge_usd() -> float:
    """Volume storage for one full cycle, counted up front."""
    return config.VOLUME_STORAGE_GIB * config.PRICE_VOLUME_GIB_MONTH


# ---------------------------------------------------------------------------
# Billing cycle
# ---------------------------------------------------------------------------
def _cycle_day(year: int, month: int, start_day: int) -> dt.date:
    """The cycle's start date in a given month; clamps for short months (e.g. 31 -> Feb 28)."""
    return dt.date(year, month, min(start_day, calendar.monthrange(year, month)[1]))


def _shift_month(year: int, month: int, delta: int) -> tuple[int, int]:
    index = year * 12 + (month - 1) + delta
    return index // 12, index % 12 + 1


def cycle_start(now: dt.datetime | None = None, start_day: int | None = None) -> dt.date:
    """Start date (UTC) of the billing cycle containing `now`."""
    now = now or dt.datetime.now(dt.UTC)
    start_day = start_day or config.BILLING_CYCLE_START_DAY
    today = now.astimezone(dt.UTC).date() if now.tzinfo else now.date()
    this_month = _cycle_day(today.year, today.month, start_day)
    if today >= this_month:
        return this_month
    y, m = _shift_month(today.year, today.month, -1)
    return _cycle_day(y, m, start_day)


def next_cycle_start(now: dt.datetime | None = None, start_day: int | None = None) -> dt.date:
    start_day = start_day or config.BILLING_CYCLE_START_DAY
    current = cycle_start(now, start_day)
    y, m = _shift_month(current.year, current.month, 1)
    return _cycle_day(y, m, start_day)


def cycle_id(now: dt.datetime | None = None) -> str:
    return cycle_start(now).isoformat()


def resets_at(now: dt.datetime | None = None) -> str:
    """ISO-8601 UTC timestamp when the next cycle (and fresh credit) begins."""
    return f"{next_cycle_start(now).isoformat()}T00:00:00Z"


# ---------------------------------------------------------------------------
# Summing and compaction (pure functions over the Dict's items)
# ---------------------------------------------------------------------------
def _split_key(key: Any) -> tuple[str, str] | None:
    if not isinstance(key, str) or ":" not in key:
        return None
    cycle, rest = key.split(":", 1)
    return (cycle, rest) if cycle and rest else None


def _usd(value: Any) -> float:
    try:
        return max(0.0, float(value["usd"]))
    except (KeyError, TypeError, ValueError):
        return 0.0


def container_costs(entries: Mapping[Any, Any], cycle: str) -> dict[str, float]:
    """Per-container cost for one cycle: max(closed record, live key)."""
    closed = entries.get(f"{cycle}:{CLOSED}") or {}
    by_id = {k: float(v) for k, v in (closed.get("by_id") or {}).items()}
    for key, value in entries.items():
        parts = _split_key(key)
        if not parts or parts[0] != cycle or parts[1] == CLOSED:
            continue
        by_id[parts[1]] = max(by_id.get(parts[1], 0.0), _usd(value))
    return by_id


def sum_spend(entries: Mapping[Any, Any], cycle: str) -> float:
    return sum(container_costs(entries, cycle).values())


def plan_compaction(
    entries: Mapping[Any, Any], now: float, current_cycle: str, stale_after: float
) -> tuple[dict[str, dict], list[str]]:
    """Return (closed records to write, keys to delete).

    - Live keys not updated for `stale_after` seconds are folded into their
      cycle's closed record and deleted.
    - Every cycle older than the previous one is dropped entirely.
    """
    cycles = {p[0] for p in map(_split_key, entries) if p} | {current_cycle}
    keep = {current_cycle, _previous_cycle_id(current_cycle)}
    writes: dict[str, dict] = {}
    deletes: list[str] = []

    for cycle in sorted(cycles):
        if cycle not in keep:
            deletes.extend(k for k in entries if (p := _split_key(k)) and p[0] == cycle)
            continue
        closed_key = f"{cycle}:{CLOSED}"
        closed = dict((entries.get(closed_key) or {}).get("by_id") or {})
        for key, value in entries.items():
            parts = _split_key(key)
            if not parts or parts[0] != cycle or parts[1] == CLOSED:
                continue
            updated = value.get("updated_at", 0) if isinstance(value, Mapping) else 0
            if now - float(updated) >= stale_after:
                closed[parts[1]] = max(float(closed.get(parts[1], 0.0)), _usd(value))
                deletes.append(key)
        if cycle == current_cycle or closed:
            # Always rewrite the current cycle's record: this is what keeps it alive.
            writes[closed_key] = {"by_id": closed, "updated_at": now}
    return writes, deletes


def _previous_cycle_id(cycle: str) -> str:
    start = dt.date.fromisoformat(cycle)
    y, m = _shift_month(start.year, start.month, -1)
    return _cycle_day(y, m, config.BILLING_CYCLE_START_DAY).isoformat()


# ---------------------------------------------------------------------------
# Modal-backed helpers
# ---------------------------------------------------------------------------
@functools.cache
def spend_dict() -> modal.Dict:
    return modal.Dict.from_name(config.SPEND_DICT, create_if_missing=True)


def total_spend(cycle: str | None = None, entries: Mapping[Any, Any] | None = None) -> float:
    """Storage charge + all container costs for the cycle (blocking)."""
    cycle = cycle or cycle_id()
    if entries is None:
        entries = dict(spend_dict().items())
    return storage_charge_usd() + sum_spend(entries, cycle)


async def total_spend_async(cycle: str | None = None) -> float:
    cycle = cycle or cycle_id()
    d = spend_dict()
    entries = {k: v async for k, v in d.items.aio()}
    return storage_charge_usd() + sum_spend(entries, cycle)


class SpendCache:
    """Caches total spend for `ttl` seconds (the gateway checks it on every chat)."""

    def __init__(
        self,
        fetch: Callable[[], Any] = total_spend_async,
        ttl: float = config.SPEND_CACHE_TTL_S,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._fetch, self._ttl, self._clock = fetch, ttl, clock
        self._value: float | None = None
        self._at = float("-inf")

    async def get(self) -> float:
        if self._value is None or self._clock() - self._at >= self._ttl:
            self._value = float(await self._fetch())
            self._at = self._clock()
        return self._value


# ---------------------------------------------------------------------------
# Per-container meter thread
# ---------------------------------------------------------------------------
class SpendMeter:
    """Writes this container's cumulative cost every `interval` seconds.

    Start it as the very first thing in container startup; stop it last.
    If the billing cycle rolls over while running, the old key gets a final
    write and a new key starts from zero for the new cycle.
    """

    def __init__(
        self,
        kind: str,
        rate: float,
        *,
        store: Any = None,
        interval: float = config.METER_INTERVAL_S,
        overhead_s: float = config.METER_STARTUP_OVERHEAD_S,
        monotonic: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
        cycle_fn: Callable[[], str] = cycle_id,
    ):
        self.kind, self.rate, self.interval = kind, rate, interval
        self._store = store
        self._mono, self._wall, self._cycle_fn = monotonic, wall, cycle_fn
        self.container_id = uuid.uuid4().hex
        self._cycle = cycle_fn()
        self._segment_start = monotonic() - overhead_s
        self._started_wall = wall()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    @property
    def key(self) -> str:
        return f"{self._cycle}:{self.container_id}"

    def _dict(self):
        if self._store is None:
            self._store = spend_dict()
        return self._store

    def cost(self) -> float:
        return max(0.0, self._mono() - self._segment_start) * self.rate

    def write(self) -> None:
        with self._lock:
            current = self._cycle_fn()
            if current != self._cycle:
                self._put()  # final write for the old cycle
                self._cycle = current
                self._segment_start = self._mono()
            self._put()

    def _put(self) -> None:
        value = {
            "usd": round(self.cost(), 6),
            "kind": self.kind,
            "started_at": self._started_wall,
            "updated_at": self._wall(),
        }
        try:
            self._dict().put(self.key, value)
        except Exception:  # never take the container down because of the meter
            log.exception("spend meter write failed for %s", self.key)

    def start(self) -> SpendMeter:
        self.write()
        self._thread = threading.Thread(target=self._run, name="spend-meter", daemon=True)
        self._thread.start()
        return self

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            self.write()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        self.write()


# ---------------------------------------------------------------------------
# Daily compaction job (CPU, a few seconds per day)
# ---------------------------------------------------------------------------
def compact(store: Any = None, now: float | None = None, items: Iterable | None = None) -> dict:
    store = store if store is not None else spend_dict()
    now = now if now is not None else time.time()
    entries = dict(items if items is not None else store.items())
    current = cycle_id(dt.datetime.fromtimestamp(now, dt.UTC))
    writes, deletes = plan_compaction(entries, now, current, config.METER_STALE_AFTER_S)
    # Write closed records before deleting live keys, so a crash in between
    # leaves duplicates (harmless under max) rather than gaps.
    for key, value in writes.items():
        store.put(key, value)
    for key in deletes:
        store.pop(key, None)
    return {
        "cycle": current,
        "written": sorted(writes),
        "deleted": len(deletes),
        "spend_usd": round(total_spend(current, {**entries, **writes}), 4),
    }


@app.function(
    image=modal.Image.debian_slim(python_version=config.PYTHON_VERSION),
    schedule=modal.Cron(config.COMPACT_SCHEDULE_CRON),
    cpu=0.125,
    memory=256,
    max_containers=1,
    timeout=120,
)
def compact_spend() -> dict:
    result = compact()
    print(result)
    return result
