"""Is the 13-week DN edge STABLE or stale/regime-dependent? And is there a real
structural UP bias at our horizon? Two tests the user asked for:

  1. RECENCY: bucket the live-rule backtest fills by week, report UP vs DN ROI
     per week — if DN's edge is front-loaded in old data and decays recently,
     the 'old data is misleading' concern is justified and the live DN losses
     are corroborated. If DN is stable +EV throughout, live is noise.
  2. BASE RATE: unconditional Up-vs-Down settlement rate of these BTC markets
     (overall + by week) — tests 'people want the asset up, so UP wins more'.
     NB: any persistent drift is already priced into the book, so a >50% UP base
     rate alone does NOT imply a tradeable UP edge — the favourite split (test 1)
     is what matters for our favourite-buying strategy.
"""
import datetime as dt
from collections import defaultdict

import numpy as np
import pandas as pd

import replay_binance as R

ARCH = R.ARCH


def wk(ep):
    d = dt.datetime.utcfromtimestamp(int(ep)).isocalendar()
    return f"{d[0]}-W{d[1]:02d}"


def roi(fs):
    dep = sum(f[0] for f in fs); pnl = sum(f[2] for f in fs)
    win = float(np.mean([f[3] for f in fs])) if fs else 0.0
    return len(fs), win, dep, pnl, (pnl / dep if dep else 0.0)


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

    by = defaultdict(lambda: {"up": [], "dn": []})
    for f in fills:
        by[wk(f[6])][f[5]].append(f)

    print(f"\n=== WEEKLY UP vs DN (live rules, realistic frictions), {len(fills)} fills ===")
    print(f"{'week':9} | {'UP n':>5} {'UP win':>7} {'UP ROI':>7} | {'DN n':>5} {'DN win':>7} {'DN ROI':>7}")
    print("-" * 64)
    for w in sorted(by):
        u = roi(by[w]["up"]); d = roi(by[w]["dn"])
        print(f"{w:9} | {u[0]:>5} {u[1]:>6.0%} {u[4]:>+7.1%} | {d[0]:>5} {d[1]:>6.0%} {d[4]:>+7.1%}")

    # first half vs second half of the archive (recency)
    weeks = sorted(by)
    half = len(weeks) // 2
    for label, ws in (("FIRST half (older)", weeks[:half]), ("SECOND half (recent)", weeks[half:])):
        up = [f for w in ws for f in by[w]["up"]]
        dn = [f for w in ws for f in by[w]["dn"]]
        u = roi(up); d = roi(dn)
        print(f"\n{label} ({ws[0]}..{ws[-1]}):")
        print(f"  UP  n={u[0]:>4} win={u[1]:.0%} ROI={u[4]:+.1%} pnl={u[3]:+.0f}")
        print(f"  DN  n={d[0]:>4} win={d[1]:.0%} ROI={d[4]:+.1%} pnl={d[3]:+.0f}")

    # ---- base rate: unconditional Up settlement rate ----
    m = pd.read_parquet(f"{ARCH}/btc_markets.parquet",
                        columns=["condition_id", "market_start", "market_end", "outcome"])
    m = m[m.outcome.isin(["Up", "Down"])].copy()
    m["dur"] = (m.market_end.astype("int64") - m.market_start.astype("int64")) // 10**9
    m["up"] = (m.outcome == "Up").astype(int)
    m5 = m[(m.dur >= 280) & (m.dur <= 320)]  # ~5m markets
    n = len(m5); up = m5.up.mean()
    se = (up * (1 - up) / n) ** 0.5
    print(f"\n=== BASE RATE (unconditional 5m settlements) ===")
    print(f"  5m markets: n={n}, Up rate={up:.3%}  (95% CI {up-1.96*se:.2%}..{up+1.96*se:.2%})")
    print(f"  z vs 50% = {(up-0.5)/se:+.2f}   (drift per 5m is tiny vs vol; and is PRICED IN)")


if __name__ == "__main__":
    main()
