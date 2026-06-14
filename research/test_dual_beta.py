"""Test 9: ideas from the other bot's logs.

1. Fee stress: re-run the late-snipe strategy under fee = 0.07*min(p,1-p)
   (their observed formula) vs 0.07*p*(1-p) (official docs / our bot).
2. Beta regime fit: fit P(up) = Phi(beta * d) per day by max likelihood on
   snapshots. Is beta regime-dependent (their claim: 0.83 vs 1.36)?
3. Dual-beta robust gate: require model edge >= theta under BOTH beta=0.83
   and beta=1.36 before taking. Does it beat the single-model gate?
"""

import math

import numpy as np
import pandas as pd
from scipy.optimize import minimize_scalar
from scipy.stats import norm

D = "research/data"
rng = np.random.default_rng(13)

tt = pd.read_parquet(f"{D}/taker_trades.parquet")
sn = pd.read_parquet(f"{D}/snapshots.parquet")

tt["fee_pq"] = 0.07 * tt.q * (1 - tt.q)
tt["fee_min"] = 0.07 * np.minimum(tt.q, 1 - tt.q)
tt["day"] = pd.to_datetime(tt.ts, unit="s").dt.date

# effective d for the bought side (model_p_side was computed with the full
# mixture; recover d_side from dist_z which is the UP-side d, sign-flip)
tt["d_side"] = np.where(tt.bought_up, tt.dist_z, -tt.dist_z)

TAIL_W, TAIL_S = 0.25, 2.5


def mix_p(d):
    return (1 - TAIL_W) * norm.cdf(d) + TAIL_W * norm.cdf(d / TAIL_S)


# ---------------- 1. fee stress on the late snipe ----------------
print("=== 1. Late snipe (5m+15m, tau<=60) under both fee formulas ===")


def run(df, fee_col, E):
    df = df[(df.model_p_side - df.q - df[fee_col]) >= E]
    if not len(df):
        return None
    rows = []
    for slug, g in df.sort_values("ts").groupby("slug"):
        spent, pnl = 0.0, 0.0
        for _, r in g.iterrows():
            if spent >= 100:
                break
            take = min(r.notional, 100 - spent)
            sh = take / r.q
            pnl += sh * (r.y - r.q - r[fee_col])
            spent += take
        rows.append({"slug": slug, "spent": spent, "pnl": pnl})
    s = pd.DataFrame(rows)
    k = len(s)
    idx = rng.integers(0, k, size=(400, k))
    boots = s.pnl.values[idx].sum(axis=1) / s.spent.values[idx].sum(axis=1)
    lo, hi = np.percentile(boots, [2.5, 97.5])
    return s.pnl.sum() / s.spent.sum(), lo, hi, k, s.spent.sum()


base = tt[(tt.kind.isin(["5m", "15m"])) & (tt.tau <= 60) & (tt.q >= 0.05) & (tt.q <= 0.95)]
for fee_col in ["fee_pq", "fee_min"]:
    for E in [0.10, 0.15]:
        r = run(base, fee_col, E)
        if r:
            ret, lo, hi, k, spent = r
            print(f"  {fee_col} edge>={E}: ret/$1 {100*ret:+.1f}% [{100*lo:+.1f},{100*hi:+.1f}] "
                  f"mkts={k} staked=${spent:,.0f}")

# ---------------- 2. per-day beta fit ----------------
print("\n=== 2. Daily beta fit: P(up)=Phi(beta*d), 5m+15m snapshots tau 30-240 ===")
s5 = sn[(sn.kind.isin(["5m", "15m"])) & (sn.tau >= 30) & (sn.tau <= 240)].copy()
s5["day"] = s5.slug.str.extract(r"-(\d+)$")[0].astype(int)
s5["day"] = pd.to_datetime(s5.day, unit="s").dt.date


def fit_beta(d, y):
    def nll(b):
        p = np.clip(norm.cdf(b * d), 1e-6, 1 - 1e-6)
        return -np.mean(y * np.log(p) + (1 - y) * np.log(1 - p))
    r = minimize_scalar(nll, bounds=(0.2, 3.0), method="bounded")
    return r.x


for day, g in s5.groupby("day"):
    if len(g) < 300:
        continue
    b = fit_beta(g.dist_z.values, g.y_up.values)
    print(f"  {day}: beta={b:.2f}  (n={len(g)}, mkts={g.slug.nunique()})")
b_all = fit_beta(s5.dist_z.values, s5.y_up.values)
print(f"  ALL:  beta={b_all:.2f}")

# ---------------- 3. dual-beta robust gate ----------------
print("\n=== 3. Dual-beta gate vs single gate (5m+15m, tau<=60, fee=p(1-p)) ===")
BL, BH = 0.83, 1.36
p_lo = mix_p(BL * tt.d_side.values)
p_hi = mix_p(BH * tt.d_side.values)
tt["p_robust"] = np.minimum(p_lo, p_hi)
tt["edge_single"] = tt.model_p_side - tt.q - tt.fee_pq
tt["edge_robust"] = tt.p_robust - tt.q - tt.fee_pq

base = tt[(tt.kind.isin(["5m", "15m"])) & (tt.tau <= 60) & (tt.q >= 0.05) & (tt.q <= 0.95)]
for name, col in [("single (current model)", "edge_single"), ("dual-beta robust", "edge_robust")]:
    for E in [0.10, 0.15]:
        sel = base[base[col] >= E]
        if len(sel) < 100:
            continue
        a = sel.groupby("slug").apply(
            lambda g: pd.Series({"wx": (g["size"] * (g.y - g.q - g.fee_pq)).sum(),
                                 "w": g["size"].sum()}), include_groups=False)
        point = a.wx.sum() / a.w.sum()
        k = len(a)
        idx = rng.integers(0, k, size=(400, k))
        boots = a.wx.values[idx].sum(axis=1) / a.w.values[idx].sum(axis=1)
        lo, hi = np.percentile(boots, [2.5, 97.5])
        print(f"  {name:24s} edge>={E}: pnl/share={100*point:+.2f}c [{100*lo:+.2f},{100*hi:+.2f}] "
              f"n={len(sel)} mkts={k}")

# what does the gate veto? trades passing single but failing robust
veto = base[(base.edge_single >= 0.15) & (base.edge_robust < 0.15)]
if len(veto) > 100:
    a = veto.groupby("slug").apply(
        lambda g: pd.Series({"wx": (g["size"] * (g.y - g.q - g.fee_pq)).sum(),
                             "w": g["size"].sum()}), include_groups=False)
    print(f"\n  vetoed-by-robust-gate trades: pnl/share={100*a.wx.sum()/a.w.sum():+.2f}c "
          f"n={len(veto)} mkts={len(a)}")
