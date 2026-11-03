"""Response time vs. concurrency, from the gateway's per-chat log lines.

Each load-test chat logs: end time, ttft_ms, latency_ms, output_tokens. A request's
interval is [end - latency, end]; its concurrency is how many admitted streams were
in flight when it started (itself included). Requests are binned by concurrency and
the p50/p95 of each measure is drawn per bin over the individual requests.

Usage: .venv/bin/python scripts/loadtest/plot.py results/gateway.log [--out docs/loadtest.svg]
Writes an SVG (light + dark via prefers-color-scheme) and prints a Markdown table.
Standard library only.
"""

import argparse
import datetime as dt
import math
import pathlib
import re
import statistics
import sys
from html import escape

LINE = re.compile(
    r"^(?P<ts>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d[+-]\d\d:\d\d) .*model-in-modal\.gateway: chat .*loadtest=1 "
    r"output_tokens=(?P<tok>\d+) ttft_ms=(?P<ttft>\d+) latency_ms=(?P<lat>\d+) status=200 result=ok"
)
BIN = 5  # streams per bin

# Reference palette (dataviz skill), slots 1-2; validated light + dark.
CSS = """
.viz { --surface:#fcfcfb; --ink:#0b0b0b; --ink2:#52514e; --muted:#898781; --grid:#e1e0d9;
       --axis:#c3c2b7; --s1:#2a78d6; --s2:#eb6834; --dot:#898781; }
@media (prefers-color-scheme: dark) {
  .viz { --surface:#1a1a19; --ink:#f0efec; --ink2:#c3c2b7; --muted:#898781; --grid:#2c2c2a;
         --axis:#383835; --s1:#3987e5; --s2:#d95926; --dot:#898781; }
}
text { font-family: system-ui, -apple-system, "Segoe UI", sans-serif; fill: var(--ink2); }
.title { font-size: 15px; font-weight: 600; fill: var(--ink); }
.tick { font-size: 11px; fill: var(--muted); font-variant-numeric: tabular-nums; }
.label { font-size: 12px; }
.note { font-size: 11px; fill: var(--muted); }
.grid { stroke: var(--grid); stroke-width: 1; }
.axis { stroke: var(--axis); stroke-width: 1; }
.req { fill: var(--dot); fill-opacity: 0.28; }
.p50 { stroke: var(--s1); } .p95 { stroke: var(--s2); }
.line { fill: none; stroke-width: 2; stroke-linejoin: round; stroke-linecap: round; }
.m50 { fill: var(--s1); } .m95 { fill: var(--s2); }
.marker { stroke: var(--surface); stroke-width: 2; }
"""


def parse(path: pathlib.Path) -> list[dict]:
    rows, seen = [], set()
    for raw in path.read_text(errors="replace").splitlines():
        m = LINE.search(raw.strip())
        if not m or raw in seen:  # log replays can repeat lines
            continue
        seen.add(raw)
        end = dt.datetime.fromisoformat(m["ts"]).timestamp() + 0.5  # logs have 1 s resolution
        lat = int(m["lat"]) / 1000
        ttft = int(m["ttft"]) / 1000
        tok = int(m["tok"])
        rows.append({"start": end - lat, "end": end, "ttft_s": ttft, "latency_s": lat,
                     "tps": tok / (lat - ttft) if tok > 1 and lat > ttft else None, "tokens": tok})
    for r in rows:
        r["conc"] = sum(1 for o in rows if o["start"] <= r["start"] < o["end"])
    return rows


def pct(values: list[float], q: float) -> float:
    if len(values) == 1:
        return values[0]
    return statistics.quantiles(values, n=100, method="inclusive")[q - 1]


def binned(rows: list[dict], key: str) -> list[tuple[float, float, float, int]]:
    """(bin centre, median, slowest-5% value, n). Slowest = p95 for times, p5 for speed."""
    slow_q = 5 if key == "tps" else 95
    out = []
    top = max(r["conc"] for r in rows)
    for lo in range(1, top + 1, BIN):
        vals = [r[key] for r in rows if lo <= r["conc"] < lo + BIN and r[key] is not None]
        if len(vals) >= 3:
            out.append((lo + (BIN - 1) / 2, pct(vals, 50), pct(vals, slow_q), len(vals)))
    return out


def nice_ticks(vmax: float, n: int = 4) -> list[float]:
    raw = vmax / n
    mag = 10 ** math.floor(math.log10(raw)) if raw > 0 else 1
    step = next(s * mag for s in (1, 2, 2.5, 5, 10) if s * mag >= raw)
    return [i * step for i in range(int(math.ceil(vmax / step)) + 1)]


def fmt(v: float) -> str:
    return f"{v:.0f}" if v >= 10 or v == 0 else f"{v:.1f}".rstrip("0").rstrip(".")


def panel(rows, key, title, unit, y0, x_max, W, H, L, R) -> str:
    pts = [(r["conc"], r[key]) for r in rows if r[key] is not None]
    bins = binned(rows, key)
    vmax = max([v for _, v in pts] + [b[1] for b in bins] + [b[2] for b in bins]) * 1.05
    ticks = nice_ticks(vmax)
    ymax = ticks[-1]
    top, bottom = y0 + 34, y0 + H - 30
    x = lambda c: L + (c / x_max) * (W - L - R)  # noqa: E731
    y = lambda v: bottom - (v / ymax) * (bottom - top)  # noqa: E731
    s = [f'<text class="title" x="{L}" y="{y0 + 18}">{escape(title)}</text>']
    for t in ticks:
        s.append(f'<line class="grid" x1="{L}" x2="{W - R}" y1="{y(t):.1f}" y2="{y(t):.1f}"/>')
        s.append(f'<text class="tick" x="{L - 8}" y="{y(t) + 4:.1f}" text-anchor="end">{fmt(t)}</text>')
    s.append(f'<text class="label" transform="translate({L - 46},{(top + bottom) / 2:.0f}) rotate(-90)" '
             f'text-anchor="middle">{escape(unit)}</text>')
    s.append(f'<line class="axis" x1="{L}" x2="{W - R}" y1="{bottom}" y2="{bottom}"/>')
    for c in range(0, int(x_max) + 1, 10):
        s.append(f'<text class="tick" x="{x(c):.1f}" y="{bottom + 16}" text-anchor="middle">{c}</text>')
    s += [f'<circle class="req" cx="{x(c):.1f}" cy="{y(v):.1f}" r="2.5"/>' for c, v in pts]
    for idx, cls in ((1, "50"), (2, "95")):
        path = " ".join(f"{'M' if i == 0 else 'L'}{x(b[0]):.1f},{y(b[idx]):.1f}" for i, b in enumerate(bins))
        s.append(f'<path class="line p{cls}" d="{path}"/>')
        s += [f'<circle class="marker m{cls}" cx="{x(b[0]):.1f}" cy="{y(b[idx]):.1f}" r="4"/>' for b in bins]
    if bins:  # direct labels at the line ends (text tokens; the end marker carries the colour)
        last = bins[-1]
        y50, y95 = y(last[1]), y(last[2])
        if abs(y95 - y50) < 14:
            y95 = y50 - 14 if y95 <= y50 else y50 + 14
        s.append(f'<text class="label" x="{x(last[0]) + 10:.1f}" y="{y50 + 4:.1f}">median</text>')
        s.append(f'<text class="label" x="{x(last[0]) + 10:.1f}" y="{y95 + 4:.1f}">slowest 5%</text>')
    return "\n".join(s)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("log", type=pathlib.Path)
    ap.add_argument("--out", type=pathlib.Path, default=pathlib.Path("docs/loadtest.svg"))
    ap.add_argument("--subtitle", default="")
    args = ap.parse_args()
    rows = parse(args.log)
    if len(rows) < 10:
        sys.exit(f"only {len(rows)} load-test chats found in {args.log}")
    W, H, L, R = 760, 240, 64, 84
    x_max = max(10, math.ceil(max(r["conc"] for r in rows) / 10) * 10)
    panels = [("ttft_s", "Time to first token", "seconds"),
              ("latency_s", "Total response time (up to 512 tokens)", "seconds"),
              ("tps", "Decode speed per user", "tokens / s")]
    top_pad = 62
    height = top_pad + H * len(panels) + 40
    body = [panel(rows, k, t, u, top_pad + i * H, x_max, W, H, L, R) for i, (k, t, u) in enumerate(panels)]
    legend_y = 40
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" class="viz" viewBox="0 0 {W} {height}" width="{W}" height="{height}" role="img" aria-labelledby="t d">
<title id="t">Response time vs. concurrent streams</title>
<desc id="d">{len(rows)} load-test requests. For each, the number of streams in flight when it started (x) against time to first token, total response time and decode speed. Lines are the median and the slowest 5% per bin of {BIN} streams; dots are individual requests. The same numbers are in the README table.</desc>
<style>{CSS}</style>
<rect width="100%" height="100%" fill="var(--surface)"/>
<text class="title" x="{L}" y="22" style="font-size:17px">Response time vs. concurrent streams</text>
<circle class="marker m50" cx="{L + 6}" cy="{legend_y}" r="4"/><text class="label" x="{L + 16}" y="{legend_y + 4}">median</text>
<circle class="marker m95" cx="{L + 84}" cy="{legend_y}" r="4"/><text class="label" x="{L + 94}" y="{legend_y + 4}">slowest 5% (p95 time, p5 speed)</text>
<circle class="req" cx="{L + 314}" cy="{legend_y}" r="3"/><text class="label" x="{L + 324}" y="{legend_y + 4}">one request</text>
<text class="note" x="{W - R}" y="{legend_y + 4}" text-anchor="end">{escape(args.subtitle)}</text>
{chr(10).join(body)}
<text class="label" x="{(L + W - R) / 2:.0f}" y="{height - 12}" text-anchor="middle">Streams in flight when the request started</text>
</svg>
"""
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(svg)
    print(f"wrote {args.out} from {len(rows)} requests (max concurrency {max(r['conc'] for r in rows)})\n")
    print("| Streams in flight | Requests | TTFT median / p95 (s) | Total median / p95 (s) | Tokens/s per user median / p5 |")
    print("|---|---|---|---|---|")
    t, l, p = binned(rows, "ttft_s"), binned(rows, "latency_s"), {b[0]: b for b in binned(rows, "tps")}
    for bt, bl in zip(t, l):
        lo = int(bt[0] - (BIN - 1) / 2)
        bp = p.get(bt[0])
        tps = f"{bp[1]:.1f} / {bp[2]:.1f}" if bp else "–"
        print(f"| {lo}–{lo + BIN - 1} | {bt[3]} | {bt[1]:.2f} / {bt[2]:.2f} | {bl[1]:.1f} / {bl[2]:.1f} | {tps} |")


if __name__ == "__main__":
    main()
