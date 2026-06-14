"""Test 8: does the late-snipe edge survive realistic latency?

A 350ms-latency bot can only capture dislocations that persist. Proxies:

P1 "persistent": qualifying print where ANOTHER qualifying print on the same
   market/side occurred 1-5s EARLIER (opportunity was alive >= 1s before; we
   could have been the second taker).
P2 "next-second requalify": qualifying print where a print on the same side
   within the NEXT 1-3s still had edge >= threshold - 0.03 (price didn't
   instantly snap back).

Also: hour-of-day breakdown and concentration check (top-market share of pnl).
"""

import numpy as np
import pandas as pd

D = "research/data"
rng = np.random.default_rng(9)

tt = pd.read_parquet(f"{D}/taker_trades.parquet")
tt["pnl"] = tt.y - tt.q - tt.fee
tt["edge_model"] = tt.model_p_side - tt.q - tt.fee

E, T = 0.15, 60
base = tt[(tt.kind.isin(["5m", "15m"])) & (tt.tau <= T) & (tt.q >= 0.05) & (tt.q <= 0.95)].copy()
qual = base[base.edge_model >= E].copy()
print(f"qualifying prints: {len(qual)} across {qual.slug.nunique()} markets")


def stat(df, name):
    if len(df) < 50:
        print(f"{name}: n too small ({len(df)})")
        return
    a = df.groupby("slug").apply(
        lambda g: pd.Series({"wx": (g["size"] * g.pnl).sum(), "w": g["size"].sum()}),
        include_groups=False)
    point = a.wx.sum() / a.w.sum()
    k = len(a)
    idx = rng.integers(0, k, size=(400, k))
    boots = a.wx.values[idx].sum(axis=1) / a.w.values[idx].sum(axis=1)
    lo, hi = np.percentile(boots, [2.5, 97.5])
    print(f"{name}: n={len(df):6d} mkts={k:4d} pnl/share={100*point:+.2f}c [{100*lo:+.2f},{100*hi:+.2f}]")


stat(qual, "ALL qualifying prints           ")

# P1: another qualifying print on same (slug, side) 1-5s earlier
qual_sorted = qual.sort_values(["slug", "bought_up", "ts"])
prev_ts = qual_sorted.groupby(["slug", "bought_up"])["ts"].shift(1)
gap = qual_sorted.ts - prev_ts
p1 = qual_sorted[(gap >= 1) & (gap <= 5)]
stat(p1, "P1 persistent (2nd taker, 1-5s) ")

# P2: any same-side print in next 1-3s that still has edge >= E-0.03
tt_sorted = tt.sort_values(["slug", "ts"])
by_mkt = {s: g for s, g in tt_sorted.groupby("slug")}
keep = []
for i, r in qual.iterrows():
    g = by_mkt[r.slug]
    nxt = g[(g.ts > r.ts) & (g.ts <= r.ts + 3) & (g.bought_up == r.bought_up)]
    if len(nxt) and (nxt.edge_model >= E - 0.03).any():
        keep.append(i)
p2 = qual.loc[keep]
stat(p2, "P2 requalifies within 3s        ")

# concentration: share of total pnl from top 10 markets
mk_pnl = (qual["size"] * qual.pnl).groupby(qual.slug).sum().sort_values()
tot = mk_pnl.sum()
print(f"\npnl concentration: total={tot:.0f}sh-c | top10 mkts={mk_pnl.tail(10).sum()/tot*100:.0f}% "
      f"| worst10={mk_pnl.head(10).sum():.0f} | positive mkts={(mk_pnl>0).mean()*100:.0f}%")

# hour-of-day (UTC)
qual["hour"] = pd.to_datetime(qual.ts, unit="s").dt.hour
h = qual.groupby(qual.hour // 4 * 4).apply(
    lambda g: np.average(g.pnl, weights=g["size"]) * 100, include_groups=False)
print("\npnl/share (c) by UTC hour block:")
print(h.round(2).to_string())

# what does the edge look like 1s later? decay estimate at threshold
print("\nedge persistence: of prints with edge>=0.15, share with a SAME-SIDE print")
for w in [1, 2, 3, 5]:
    cnt = 0
    tot_n = 0
    for i, r in qual.sample(min(3000, len(qual)), random_state=1).iterrows():
        g = by_mkt[r.slug]
        nxt = g[(g.ts > r.ts) & (g.ts <= r.ts + w) & (g.bought_up == r.bought_up)]
        tot_n += 1
        if len(nxt):
            cnt += 1
    print(f"  within {w}s: {100*cnt/tot_n:.0f}% (any print at all on same side)")
