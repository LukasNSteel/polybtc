"""Test 7: turn the significant cells into concrete strategies and stress them.

A) LATE SNIPE  — take prints in the final tau_max seconds where the replicated
   model edge (model_p_side - q - fee) >= threshold. Cap $ per market.
B) FAVORITE    — buy the leading side at q in [q_lo, q_hi] in the EARLY part
   of the window (tau >= frac * window). One entry per market.
C) SCALP 15m/1h — final 15-60s, q in [0.90, 0.99] (replaces 5m-heavy scalp).

For each: per-market PnL, daily breakdown, win rate, capacity (avg available
notional), and cluster-bootstrapped CI of return per $1.
"""

import numpy as np
import pandas as pd

D = "research/data"
rng = np.random.default_rng(5)

tt = pd.read_parquet(f"{D}/taker_trades.parquet")
tt["pnl"] = tt.y - tt.q - tt.fee
tt["edge_model"] = tt.model_p_side - tt.q - tt.fee
tt["day"] = pd.to_datetime(tt.ts, unit="s").dt.date
WINDOW = {"5m": 300, "15m": 900, "1h": 3600, "4h": 14400}
tt["window"] = tt.kind.map(WINDOW)


def run_strategy(df, name, cap_usd=100.0):
    """Fill prints in order per market until $cap is spent. Report stats."""
    if not len(df):
        print(f"{name}: no trades")
        return
    rows = []
    for slug, g in df.sort_values("ts").groupby("slug"):
        spent = 0.0
        pnl = 0.0
        for _, r in g.iterrows():
            if spent >= cap_usd:
                break
            take = min(r.notional, cap_usd - spent)
            sh = take / r.q
            pnl += sh * r.pnl
            spent += take
        rows.append({"slug": slug, "day": g.day.iloc[0], "kind": g.kind.iloc[0],
                     "spent": spent, "pnl": pnl})
    s = pd.DataFrame(rows)
    tot_spent, tot_pnl = s.spent.sum(), s.pnl.sum()
    ret = tot_pnl / tot_spent
    # bootstrap over markets
    k = len(s)
    idx = rng.integers(0, k, size=(400, k))
    boots = s.pnl.values[idx].sum(axis=1) / s.spent.values[idx].sum(axis=1)
    lo, hi = np.percentile(boots, [2.5, 97.5])
    win = (s.pnl > 0).mean()
    print(f"\n--- {name} ---")
    print(f"markets traded: {k} | total staked ${tot_spent:,.0f} | pnl ${tot_pnl:+,.0f} "
          f"| ret/$1 {100*ret:+.2f}% [{100*lo:+.2f},{100*hi:+.2f}] | mkt win% {100*win:.0f}")
    d = s.groupby("day").agg(n=("pnl", "size"), spent=("spent", "sum"), pnl=("pnl", "sum"))
    d["ret%"] = (100 * d.pnl / d.spent).round(2)
    print(d.round(0).to_string())
    return s


print("=" * 70)
print("A) LATE SNIPE: tau <= T, model edge >= E, 0.05 <= q <= 0.95, $100/market")
print("=" * 70)
for kinds in [("5m",), ("15m",), ("5m", "15m")]:
    for T, E in [(60, 0.08), (60, 0.15), (90, 0.10)]:
        df = tt[tt.kind.isin(kinds) & (tt.tau <= T) & (tt.edge_model >= E)
                & (tt.q >= 0.05) & (tt.q <= 0.95)]
        run_strategy(df, f"snipe {'+'.join(kinds)} tau<={T} edge>={E}")

print()
print("=" * 70)
print("B) FAVORITE: q in [0.90,0.98], early window (tau >= 0.4*window), $100/mkt")
print("=" * 70)
for kinds in [("5m",), ("15m",), ("1h",), ("4h",)]:
    df = tt[tt.kind.isin(kinds) & (tt.q >= 0.90) & (tt.q <= 0.98)
            & (tt.tau >= 0.4 * tt.window)]
    run_strategy(df, f"favorite {'+'.join(kinds)}")

print()
print("=" * 70)
print("C) SCALP: tau in [15,60], q in [0.90,0.99]  (current bot: 45s, 0.90-0.999, all kinds)")
print("=" * 70)
for kinds in [("5m",), ("15m",), ("1h",), ("15m", "1h")]:
    df = tt[tt.kind.isin(kinds) & (tt.tau >= 15) & (tt.tau <= 60)
            & (tt.q >= 0.90) & (tt.q <= 0.99)]
    run_strategy(df, f"scalp {'+'.join(kinds)} tau 15-60 q 0.90-0.99")

# current config for comparison: tau<=45, q 0.90-0.999, model_p>=0.997 approximated by q>=0.99
print("\n(current-config comparison: all kinds, tau<=45, q 0.90-0.999)")
df = tt[(tt.tau <= 45) & (tt.q >= 0.90) & (tt.q <= 0.999)]
run_strategy(df, "scalp CURRENT-ish all kinds")

print()
print("=" * 70)
print("CAPACITY: available taker notional per market in the snipe bucket")
print("=" * 70)
df = tt[(tt.tau <= 60) & (tt.edge_model >= 0.10) & (tt.q >= 0.05) & (tt.q <= 0.95)]
cap = df.groupby(["kind", "slug"]).notional.sum()
print(cap.groupby("kind").describe().round(0))
