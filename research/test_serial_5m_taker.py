"""DEFINITIVE executable test of the 5m mean-reversion edge.

Joins the prior 5m market's realized move to the EARLY-window taker prints of the
next market, and measures the real per-share EV of buying the REVERSION side
(after a big prior UP move -> buy DOWN early; after big DOWN -> buy UP early) at
actual executable prices, net of the real p(1-p) fee, with market-cluster CIs.

Prior move comes from each market's OWN open_price/close (exactly how it resolved,
no Binance basis mismatch). 'reversion side won' uses the side-relative y in the
tape (y=1 iff the side the taker bought won). ~2.2M 5m prints over the study.

Run: python research/test_serial_5m_taker.py
"""
import numpy as np
import pandas as pd

rng = np.random.default_rng(7)
tt = pd.read_parquet("research/data/taker_trades.parquet")
tt = tt[tt.kind == "5m"].copy()
tt["epoch"] = tt.slug.str.extract(r"-(\d+)$").astype(int)


def boot_ci(pnl, slug, n=500):
    d = pd.DataFrame({"x": pnl, "slug": slug})
    a = d.groupby("slug").agg(s=("x", "sum"), c=("x", "size"))
    s, c = a.s.values, a.c.values
    k = len(s)
    idx = rng.integers(0, k, size=(n, k))
    means = s[idx].sum(1) / c[idx].sum(1)
    return s.sum() / c.sum(), *np.percentile(means, [2.5, 97.5])


# prior candle move from the Binance 5m cache (real candle open/close, keyed by
# candle-open epoch). The parquet 'close' column is spot-at-trade, not the
# resolved candle close, so we must NOT derive the move from it.
cand = pd.read_csv("research/data/btc_5m_240d.csv")  # ts, open, close, bps
prior = dict(zip(cand.ts.astype(int), cand.bps))
tt["prior_bps"] = (tt.epoch - 300).map(prior)
tt = tt.dropna(subset=["prior_bps"])

print(f"5m prints with a known prior candle: {len(tt):,} over {tt.slug.nunique()} markets")

for TAU in (60, 120):
    early = tt[tt.tau >= 300 - TAU].copy()   # first `TAU` seconds of the window
    # the REVERSION side: prior up -> we want DOWN buys; prior down -> UP buys
    early["is_rev"] = np.where(early.prior_bps > 0, ~early.bought_up, early.bought_up)
    print(f"\n{'='*86}\nENTER IN FIRST {TAU}s  (n={len(early):,}, mean entry q="
          f"{early.q.mean():.3f})\n{'='*86}")
    edges = [-1e9, -40, -20, -10, -3, 3, 10, 20, 40, 1e9]
    labels = ["<-40", "-40..-20", "-20..-10", "-10..-3", "-3..3",
              "3..10", "10..20", "20..40", ">40"]
    early["bin"] = pd.cut(early.prior_bps, edges, labels=labels)
    print(f"{'prior move':>11} {'rev side':>9} {'n':>7} {'mkts':>5} {'entry q':>8} "
          f"{'REVERSION pnl/sh':>22}")
    print("-" * 86)
    for lab in labels:
        b = early[(early.bin == lab) & early.is_rev]
        if len(b) < 100 or b.slug.nunique() < 12:
            print(f"{lab:>11} {'':>9} {len(b):>7}  (too few)")
            continue
        side = "DOWN" if lab.startswith(("3", "1", "2", ">")) and not lab.startswith("-") else "UP"
        m, lo, hi = boot_ci((b.y - b.q - b.fee).values, b.slug.values)
        star = "*" if (lo > 0 or hi < 0) else " "
        print(f"{lab:>11} {side:>9} {len(b):>7} {b.slug.nunique():>5} {b.q.mean():>8.3f} "
              f"{100*m:>+8.2f}c [{100*lo:+.2f},{100*hi:+.2f}]{star}")

    # net effect: pool all |prior|>20bps reversion entries (the only fee-clearing band)
    big = early[(early.prior_bps.abs() > 20) & early.is_rev]
    if len(big) > 50:
        m, lo, hi = boot_ci((big.y - big.q - big.fee).values, big.slug.values)
        star = "*" if (lo > 0 or hi < 0) else " "
        # and the MOMENTUM side for contrast (buy WITH the prior move)
        mom = early[(early.prior_bps.abs() > 20) & ~early.is_rev]
        mm, ml, mh = boot_ci((mom.y - mom.q - mom.fee).values, mom.slug.values)
        print(f"\n  POOLED |prior|>20bps  REVERSION n={len(big):6d} mkts={big.slug.nunique():4d}"
              f"  pnl/sh={100*m:+.2f}c [{100*lo:+.2f},{100*hi:+.2f}]{star}")
        print(f"  POOLED |prior|>20bps  MOMENTUM  n={len(mom):6d} mkts={mom.slug.nunique():4d}"
              f"  pnl/sh={100*mm:+.2f}c [{100*ml:+.2f},{100*mh:+.2f}]")
print("\n* = 95% market-cluster CI excludes 0. pnl already net the p(1-p) taker fee.")


if __name__ == "__main__":
    pass
