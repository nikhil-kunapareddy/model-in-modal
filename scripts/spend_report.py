"""Print the spend meter's Dict contents and the current cycle's total. Read-only.

Usage: .venv/bin/python scripts/spend_report.py
"""

import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from backend import config, meter  # noqa: E402


def main() -> None:
    entries = dict(meter.spend_dict().items())
    cycle = meter.cycle_id()
    now = time.time()
    print(f"cycle {cycle}  resets {meter.resets_at()}  soft cap ${config.SOFT_CAP_USD:.2f}")
    for key in sorted(entries, key=str):
        v = entries[key]
        if str(key).endswith(":closed"):
            by_id = v.get("by_id", {})
            print(f"  {key:<48} closed: {len(by_id)} containers, ${sum(by_id.values()):.4f}")
        else:
            age = now - float(v.get("updated_at", 0))
            ran = float(v.get("updated_at", 0)) - float(v.get("started_at", 0))
            print(f"  {key:<48} {v.get('kind', '?'):<8} ${v.get('usd', 0):.4f}  ran {ran:6.0f}s  updated {age:6.0f}s ago")
    containers = meter.sum_spend(entries, cycle)
    print(f"containers ${containers:.4f} + storage ${meter.storage_charge_usd():.2f} = total ${containers + meter.storage_charge_usd():.4f}")


if __name__ == "__main__":
    main()
