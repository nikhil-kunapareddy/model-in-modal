"""Sample live concurrency during a load test (run alongside k6). Prints no secrets.

Every few seconds records: streams in flight (size of the gateway's global
stream set in Upstash) and live GPU / gateway containers (from the spend meter). Writes
results/monitor.csv and prints the peaks when stopped (Ctrl-C) or after --minutes.

Usage: .venv/bin/python scripts/loadtest/monitor.py [--minutes 20]
"""

import argparse
import asyncio
import csv
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from backend import config, meter, ratelimit  # noqa: E402


def load_env() -> dict[str, str]:
    env = {}
    for line in (ROOT / ".env").read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def containers() -> dict[str, int]:
    """Live containers by kind, from the spend meter (each writes every 30 s)."""
    now = time.time()
    counts: dict[str, int] = {}
    for _, v in meter.spend_dict().items():
        if isinstance(v, dict) and now - float(v.get("updated_at", 0)) < 45:
            kind = "gpu" if str(v.get("kind", "")).startswith("gpu") else str(v.get("kind"))
            counts[kind] = counts.get(kind, 0) + 1
    return counts


async def main() -> None:
    from upstash_redis.asyncio import Redis

    parser = argparse.ArgumentParser()
    parser.add_argument("--minutes", type=float, default=20)
    args = parser.parse_args()
    env = load_env()
    redis = Redis(env["UPSTASH_REDIS_REST_URL"], env["UPSTASH_REDIS_REST_TOKEN"], allow_telemetry=False)
    out = ROOT / "scripts/loadtest/results/monitor.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    peak_streams, peak_gpu, last_c, last_t = 0, 0, {}, 0.0
    end = time.time() + args.minutes * 60
    with out.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["utc", "streams_in_flight", "gpu_containers", "gateway_containers"])
        try:
            while time.time() < end:
                now = time.time()
                tx = redis.multi()
                tx.zremrangebyscore(ratelimit.GLOBAL_STREAMS_KEY, "-inf", now - config.STREAM_SLOT_TTL_S)
                tx.zcard(ratelimit.GLOBAL_STREAMS_KEY)
                streams = int((await tx.exec())[1])
                if now - last_t >= 15:  # the CLI call is slow; sample containers less often
                    last_c, last_t = await asyncio.to_thread(containers), now  # blocking Modal call
                peak_streams = max(peak_streams, streams)
                peak_gpu = max(peak_gpu, last_c.get("gpu", 0))
                row = [time.strftime("%H:%M:%S", time.gmtime(now)), streams, last_c.get("gpu", 0), last_c.get("gateway", 0)]
                w.writerow(row)
                f.flush()
                print(*row, flush=True)
                await asyncio.sleep(3)
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
    await redis.close()
    print(f"PEAK streams_in_flight={peak_streams} gpu_containers={peak_gpu}")


if __name__ == "__main__":
    asyncio.run(main())
