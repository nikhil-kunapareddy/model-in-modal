import asyncio
import datetime as dt

import pytest

from backend import config, meter

UTC = dt.UTC


def at(y, m, d, h=12):
    return dt.datetime(y, m, d, h, tzinfo=UTC)


class FakeDict(dict):
    """Stands in for modal.Dict: put/pop/items like the real thing."""

    def put(self, key, value):
        self[key] = value
        return True


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


# ---------------------------------------------------------------- rates
def test_gpu_rates_match_modal_pricing():
    # GPU + 2 physical cores + 16 GiB, per modal.com/pricing
    cpu_mem = 2 * 0.0000131 + 16 * 0.00000222
    assert meter.GPU_RATES["L4"] == pytest.approx(0.000222 + cpu_mem)
    assert meter.GPU_RATES["L4"] * 3600 == pytest.approx(1.0214, abs=1e-4)
    assert meter.GPU_RATES["L40S"] == pytest.approx(0.000542 + cpu_mem)
    assert meter.GPU_RATES["L40S"] * 3600 == pytest.approx(2.1734, abs=1e-4)
    assert set(meter.GPU_RATES) == set(config.GPU_TYPES)


@pytest.mark.parametrize(
    "name, expected",
    [("NVIDIA L4", "L4"), ("NVIDIA L40S", "L40S"), ("nvidia l40s", "L40S"),
     ("NVIDIA A10", None), ("", None), ("NVIDIA L40", None)],
)
def test_gpu_type_from_name(name, expected):
    assert meter.gpu_type_from_name(name) == expected


def test_unknown_gpu_is_metered_at_highest_rate():
    assert meter.gpu_rate_for("NVIDIA L40S") == ("L40S", meter.GPU_RATES["L40S"])
    assert meter.gpu_rate_for("NVIDIA H100 80GB HBM3") == ("unknown", max(meter.GPU_RATES.values()))


def test_gateway_rate_uses_gateway_config():
    expected = (
        config.GATEWAY_CPU_CORES * config.PRICE_CPU_CORE_PER_S
        + config.GATEWAY_MEMORY_MIB / 1024 * config.PRICE_MEMORY_GIB_PER_S
    ) * config.GATEWAY_REGION_MULTIPLIER
    assert meter.GATEWAY_RATE == pytest.approx(expected)


def test_storage_charge():
    assert meter.storage_charge_usd() == pytest.approx(20 * 0.09)


# ---------------------------------------------------------------- cycle
@pytest.mark.parametrize(
    "now, expected",
    [
        (at(2026, 11, 2), dt.date(2026, 10, 3)),  # day before start day
        (at(2026, 11, 3, 0), dt.date(2026, 11, 3)),  # start day, midnight
        (at(2026, 11, 30), dt.date(2026, 11, 3)),
        (at(2027, 1, 2), dt.date(2026, 12, 3)),  # crosses a year boundary
        (at(2027, 1, 3), dt.date(2027, 1, 3)),
    ],
)
def test_cycle_start_day_3(now, expected):
    assert meter.cycle_start(now, 3) == expected


@pytest.mark.parametrize(
    "now, expected",
    [
        (at(2027, 2, 15), dt.date(2027, 1, 31)),
        (at(2027, 2, 28), dt.date(2027, 2, 28)),  # Feb has no 31st: clamp
        (at(2027, 3, 1), dt.date(2027, 2, 28)),
        (at(2027, 3, 30), dt.date(2027, 2, 28)),
        (at(2027, 3, 31), dt.date(2027, 3, 31)),
        (at(2027, 4, 30), dt.date(2027, 4, 30)),  # April has 30 days
        (at(2028, 2, 29), dt.date(2028, 2, 29)),  # leap year
        (at(2028, 2, 28), dt.date(2028, 1, 31)),
    ],
)
def test_cycle_start_day_31_short_months(now, expected):
    assert meter.cycle_start(now, 31) == expected


@pytest.mark.parametrize(
    "now, day, expected",
    [
        (at(2026, 11, 2), 3, dt.date(2026, 11, 3)),
        (at(2026, 11, 3), 3, dt.date(2026, 12, 3)),
        (at(2026, 12, 25), 3, dt.date(2027, 1, 3)),
        (at(2027, 1, 31), 31, dt.date(2027, 2, 28)),
        (at(2027, 2, 28), 31, dt.date(2027, 3, 31)),
        (at(2027, 1, 30), 30, dt.date(2027, 2, 28)),
    ],
)
def test_next_cycle_start(now, day, expected):
    assert meter.next_cycle_start(now, day) == expected


def test_cycle_start_uses_utc():
    # 23:30 on Nov 2 in Los Angeles is already Nov 3 in UTC.
    la = dt.timezone(dt.timedelta(hours=-8))
    assert meter.cycle_start(dt.datetime(2026, 11, 2, 23, 30, tzinfo=la), 3) == dt.date(2026, 11, 3)


def test_cycle_id_and_resets_at_format():
    assert meter.cycle_id(at(2026, 11, 20)) == "2026-11-03"
    assert meter.resets_at(at(2026, 11, 20)) == "2026-12-03T00:00:00Z"


# ---------------------------------------------------------------- summing
def test_sum_spend_live_keys_only_for_cycle():
    entries = {
        "2026-11-03:a": {"usd": 1.5},
        "2026-11-03:b": {"usd": 2.25},
        "2026-10-03:c": {"usd": 9.0},  # previous cycle: ignored
        "garbage": {"usd": 100},  # malformed: ignored
        42: {"usd": 100},
        "2026-11-03:bad": {"nope": 1},  # missing usd: counts as 0
    }
    assert meter.sum_spend(entries, "2026-11-03") == pytest.approx(3.75)


def test_sum_spend_closed_and_live_take_max_per_container():
    entries = {
        "2026-11-03:closed": {"by_id": {"a": 1.0, "b": 2.0}},
        "2026-11-03:a": {"usd": 1.5},  # resurrected / still writing: larger wins
        "2026-11-03:b": {"usd": 0.5},  # stale smaller copy: closed wins
        "2026-11-03:c": {"usd": 0.25},
    }
    assert meter.sum_spend(entries, "2026-11-03") == pytest.approx(1.5 + 2.0 + 0.25)


def test_total_spend_adds_storage():
    entries = {"2026-11-03:a": {"usd": 1.0}}
    assert meter.total_spend("2026-11-03", entries) == pytest.approx(1.0 + 1.8)


# ---------------------------------------------------------------- compaction
def test_plan_compaction_folds_stale_keeps_fresh_and_drops_old_cycles():
    now = 10_000.0
    entries = {
        "2026-11-03:closed": {"by_id": {"old": 0.5}, "updated_at": 0},
        "2026-11-03:stale": {"usd": 1.0, "updated_at": now - 3600},
        "2026-11-03:fresh": {"usd": 2.0, "updated_at": now - 10},
        "2026-10-03:prev": {"usd": 3.0, "updated_at": now - 99999},  # previous cycle kept (folded)
        "2026-09-03:ancient": {"usd": 4.0, "updated_at": 0},  # 2 cycles back: deleted
        "2026-09-03:closed": {"by_id": {"x": 1.0}},
    }
    writes, deletes = meter.plan_compaction(entries, now, "2026-11-03", stale_after=900)

    assert writes["2026-11-03:closed"]["by_id"] == {"old": 0.5, "stale": 1.0}
    assert writes["2026-11-03:closed"]["updated_at"] == now
    assert writes["2026-10-03:closed"]["by_id"] == {"prev": 3.0}
    assert sorted(deletes) == sorted(
        ["2026-11-03:stale", "2026-10-03:prev", "2026-09-03:ancient", "2026-09-03:closed"]
    )
    assert "2026-11-03:fresh" not in deletes


def test_compaction_preserves_total():
    now = 10_000.0
    store = FakeDict(
        {
            "2026-11-03:a": {"usd": 1.0, "updated_at": now - 3600},
            "2026-11-03:b": {"usd": 2.0, "updated_at": now - 5},
        }
    )
    before = meter.sum_spend(store, "2026-11-03")
    now_ts = dt.datetime(2026, 11, 20, tzinfo=UTC).timestamp()
    # shift updated_at so "a" is stale relative to now_ts and "b" is fresh
    store["2026-11-03:a"]["updated_at"] = now_ts - 3600
    store["2026-11-03:b"]["updated_at"] = now_ts - 5
    result = meter.compact(store=store, now=now_ts)

    assert "2026-11-03:a" not in store
    assert "2026-11-03:b" in store
    assert store["2026-11-03:closed"]["by_id"] == {"a": 1.0}
    assert meter.sum_spend(store, "2026-11-03") == pytest.approx(before)
    assert result["deleted"] == 1
    assert result["spend_usd"] == pytest.approx(before + 1.8)


def test_compaction_always_refreshes_current_cycle_record():
    now_ts = dt.datetime(2026, 11, 20, tzinfo=UTC).timestamp()
    store = FakeDict()
    meter.compact(store=store, now=now_ts)
    assert store["2026-11-03:closed"] == {"by_id": {}, "updated_at": now_ts}


def test_compaction_is_idempotent():
    now_ts = dt.datetime(2026, 11, 20, tzinfo=UTC).timestamp()
    store = FakeDict({"2026-11-03:a": {"usd": 1.0, "updated_at": now_ts - 3600}})
    meter.compact(store=store, now=now_ts)
    snapshot = meter.sum_spend(store, "2026-11-03")
    meter.compact(store=store, now=now_ts + 60)
    assert meter.sum_spend(store, "2026-11-03") == pytest.approx(snapshot) == pytest.approx(1.0)


# ---------------------------------------------------------------- SpendMeter
def make_meter(store, mono, wall, cycle="2026-11-03", overhead=30, rate=0.01):
    cycle_box = {"id": cycle}
    m = meter.SpendMeter(
        "gpu",
        rate,
        store=store,
        interval=3600,  # thread never ticks during tests
        overhead_s=overhead,
        monotonic=mono,
        wall=wall,
        cycle_fn=lambda: cycle_box["id"],
    )
    return m, cycle_box


def test_meter_writes_immediately_with_overhead_and_on_stop():
    store, mono, wall = FakeDict(), Clock(1000.0), Clock(5000.0)
    m, _ = make_meter(store, mono, wall)
    m.start()
    assert store[m.key]["usd"] == pytest.approx(30 * 0.01)  # overhead only
    assert store[m.key]["kind"] == "gpu"
    assert m.key.startswith("2026-11-03:") and len(m.key.split(":")[1]) == 32

    mono.t += 100
    wall.t += 100
    m.write()
    assert store[m.key]["usd"] == pytest.approx(130 * 0.01)
    assert store[m.key]["updated_at"] == 5100.0

    mono.t += 20
    m.stop()
    assert store[m.key]["usd"] == pytest.approx(150 * 0.01)


def test_meter_cycle_rollover_starts_new_key():
    store, mono, wall = FakeDict(), Clock(0.0), Clock(0.0)
    m, cycle = make_meter(store, mono, wall, overhead=0, rate=1.0)
    m.start()
    old_key = m.key
    mono.t = 50
    cycle["id"] = "2026-12-03"
    m.write()
    assert store[old_key]["usd"] == pytest.approx(50)  # final write for old cycle
    assert m.key == f"2026-12-03:{m.container_id}"
    assert store[m.key]["usd"] == pytest.approx(0)
    mono.t = 70
    m.write()
    assert store[m.key]["usd"] == pytest.approx(20)


def test_meter_survives_store_errors():
    class Broken:
        def put(self, *_):
            raise RuntimeError("dict unavailable")

    m, _ = make_meter(Broken(), Clock(), Clock())
    m.write()  # must not raise


def test_meter_thread_ticks():
    store = FakeDict()
    m = meter.SpendMeter("gateway", 1.0, store=store, interval=0.01, overhead_s=0)
    m.start()
    import time

    time.sleep(0.1)
    m.stop()
    assert store[m.key]["usd"] > 0
    assert store[m.key]["kind"] == "gateway"


# ---------------------------------------------------------------- SpendCache
def test_spend_cache_ttl():
    calls = []
    clock = Clock(0.0)

    async def fetch():
        calls.append(1)
        return 5.0 + len(calls)

    cache = meter.SpendCache(fetch=fetch, ttl=30, clock=clock)

    async def run():
        a = await cache.get()
        clock.t = 29
        b = await cache.get()
        clock.t = 30
        c = await cache.get()
        return a, b, c

    a, b, c = asyncio.run(run())
    assert (a, b, c) == (6.0, 6.0, 7.0)
    assert len(calls) == 2
