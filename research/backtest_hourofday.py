"""Does the hour-of-day effect seen in 20 live fills (15-24 UTC weak, 08-15 UTC
strong) survive on the 13-week archive? This is the only way to tell signal from
a 20-fill mirage. Buckets backtest fills by UTC fire-hour (end_ep - t_rem) under
the live 5m config, UP-only (matching current live) and both-sides (more n).
"""
import time
from collections import defaultdict

import numpy as np
import replay_binance as R


def stats(fs):
    if not fs:
        return (0, 0.0, 0.0, 0.0)
    dep = sum(f[0] for f in fs); pnl = sum(f[2] for f in fs)
    return (len(fs), float(np.mean([f[3] for f in fs])), pnl,
            pnl / dep if dep else 0.0)


def fire_hour(f):
    return time.gmtime(int(f[6]) - int(f[9])).tm_hour


def report(fills, label):
    print(f"\n=== {label}: {len(fills)} fills ===")
    # 3-bucket split matching the live exploration
    buckets = [("08-15 UTC (EU/AM)", range(8, 15)),
               ("15-24 UTC (US PM)", range(15, 24)),
               ("00-08 UTC (overnight)", range(0, 8))]
    print(f"  {'window':24} {'n':>5} {'win%':>6} {'pnl$':>8} {'ROI/$':>7}")
    for name, hrs in buckets:
        sub = [f for f in fills if fire_hour(f) in hrs]
        n, w, p, roi = stats(sub)
        print(f"  {name:24} {n:>5} {w:>6.1%} {p:>+8.0f} {roi:>+7.1%}")
    # finer per-hour ROI sparkline
    print("  per-hour ROI/$:")
    by = defaultdict(list)
    for f in fills:
        by[fire_hour(f)].append(f)
    for h in range(24):
        n, w, p, roi = stats(by.get(h, []))
        bar = ("+" if roi > 0 else "-") * min(20, int(abs(roi) * 200))
        print(f"    {h:02d}h  n={n:>4}  win={w:>4.0%}  ROI={roi:>+6.1%}  {bar}")


def main():
    print("loading...", flush=True)
    base, price, vol, obi = R.load_binance()
    ticks = R.load_ticks()
    live = dict(min_ask=0.50, max_ask=0.80, min_edge=0.10, max_edge=0.30,
                max_take_usd=10, max_position_usd=10, max_exposure_usd=60,
                cooldown=10, no_scale=True, kind_only="5m", dist_sigma_min=0.50,
                min_t_rem=30, max_t_rem=90, trend_filter_sigma=1.0,
                race_loss=0.20, capture=0.30, contention=True, feedlag=True)
    fills, _ = R.run(live, base, price, vol, ticks, obi=obi)
    report([f for f in fills if f[5] == "up"], "UP-only 5m (matches live)")
    report(fills, "BOTH sides 5m (more n)")


if __name__ == "__main__":
    main()
