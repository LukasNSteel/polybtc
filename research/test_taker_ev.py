"""Tests 1-3 on real executions.

Every row of taker_trades.parquet is a real taker execution normalized to
"bought side X at price q". EV per $1 staked = (y - q - fee)/q. We aggregate
with market-cluster bootstrap CIs (trades within one window are correlated).

Test 1: calibration / favorite-longshot bias by (kind, tau, price)
Test 2: does the bot's model edge (model_p_side - q - fee) predict taker EV?
Test 3: late-window scalp surface (tau small, q high)
"""

import numpy as np
import pandas as pd

D = "research/data"
rng = np.random.default_rng(7)

tt = pd.read_parquet(f"{D}/taker_trades.parquet")
tt["pnl"] = tt.y - tt.q - tt.fee            # per share
tt["ev1"] = tt.pnl / tt.q                   # per $1 staked
tt["edge_model"] = tt.model_p_side - tt.q - tt.fee
print(f"trades: {len(tt)}, markets: {tt.slug.nunique()}")
print(tt.groupby("kind").agg(n=("y", "size"), mkts=("slug", "nunique"),
                             notional=("notional", "sum")).round(0))


def boot_ci(df, col="pnl", w="size", n=400):
    """Cluster bootstrap over markets via per-slug aggregates (fast)."""
    wx = df[w].values * df[col].values
    agg = pd.DataFrame({"wx": wx, "w": df[w].values, "slug": df.slug.values})
    a = agg.groupby("slug").sum()
    num, den = a.wx.values, a.w.values
    point = num.sum() / den.sum()
    k = len(num)
    idx = rng.integers(0, k, size=(n, k))
    means = num[idx].sum(axis=1) / den[idx].sum(axis=1)
    lo, hi = np.percentile(means, [2.5, 97.5])
    return point, lo, hi


def surface(df, taus, prices, label):
    print(f"\n=== {label}: mean taker pnl per share (cents), share-weighted ===")
    print("rows=tau bucket, cols=price bucket; * = 95% CI excludes 0")
    hdr = "tau\\q      " + "".join(f"{f'{a:.2f}-{b:.2f}':>14s}" for a, b in prices)
    print(hdr)
    for tlo, thi in taus:
        line = f"{tlo:>4d}-{thi:<5d}"
        for plo, phi in prices:
            b = df[(df.tau >= tlo) & (df.tau < thi) & (df.q >= plo) & (df.q < phi)]
            if len(b) < 80 or b.slug.nunique() < 15:
                line += f"{'-':>14s}"
                continue
            m, lo, hi = boot_ci(b)
            star = "*" if (lo > 0 or hi < 0) else " "
            line += f"{100*m:>+8.2f}{star} n{len(b)//1000}k" if len(b) >= 1000 else f"{100*m:>+8.2f}{star} n{len(b)}"
        print(line)


# ---------- Test 1: full surface per kind ----------
PRICES = [(0.02, 0.2), (0.2, 0.4), (0.4, 0.6), (0.6, 0.8), (0.8, 0.9),
          (0.9, 0.97), (0.97, 1.0)]
TAUS = {
    "5m": [(0, 30), (30, 60), (60, 120), (120, 200), (200, 300)],
    "15m": [(0, 60), (60, 180), (180, 420), (420, 900)],
    "1h": [(0, 120), (120, 600), (600, 1800), (1800, 3600)],
    "4h": [(0, 600), (600, 3600), (3600, 14400)],
}
for kind in ["5m", "15m", "1h", "4h"]:
    df = tt[tt.kind == kind]
    if len(df):
        surface(df, TAUS[kind], PRICES, f"Test 1 [{kind}] taker EV surface")

# ---------- Test 2: model edge buckets ----------
print("\n\n=== Test 2: realized taker pnl by model edge bucket (sniper rule test) ===")
EDGES = [(-1, -0.15), (-0.15, -0.08), (-0.08, -0.03), (-0.03, 0.03),
         (0.03, 0.08), (0.08, 0.15), (0.15, 0.25), (0.25, 1.0)]
for kind in ["5m", "15m", "1h", "4h"]:
    df = tt[(tt.kind == kind) & (tt.q >= 0.05) & (tt.q <= 0.95)]
    print(f"\n[{kind}]  (edge = model_p_side - q - fee; current sniper takes 0.08..0.25)")
    for lo, hi in EDGES:
        b = df[(df.edge_model >= lo) & (df.edge_model < hi)]
        if len(b) < 100:
            continue
        m, l, h = boot_ci(b)
        star = "*" if (l > 0 or h < 0) else " "
        print(f"  edge {lo:+.2f}..{hi:+.2f}: n={len(b):6d} mkts={b.slug.nunique():4d} "
              f"q={np.average(b.q, weights=b['size']):.3f} "
              f"pnl/share={100*m:+.2f}c [{100*l:+.2f},{100*h:+.2f}]{star}")

# same but only in the final phase where the model claims to be sharpest
print("\n--- Test 2b: model edge buckets, tau <= 60s only ---")
for kind in ["5m", "15m"]:
    df = tt[(tt.kind == kind) & (tt.q >= 0.05) & (tt.q <= 0.95) & (tt.tau <= 60)]
    print(f"[{kind}]")
    for lo, hi in EDGES:
        b = df[(df.edge_model >= lo) & (df.edge_model < hi)]
        if len(b) < 80:
            continue
        m, l, h = boot_ci(b)
        star = "*" if (l > 0 or h < 0) else " "
        print(f"  edge {lo:+.2f}..{hi:+.2f}: n={len(b):6d} mkts={b.slug.nunique():4d} "
              f"pnl/share={100*m:+.2f}c [{100*l:+.2f},{100*h:+.2f}]{star}")

# ---------- Test 3: scalp surface ----------
print("\n\n=== Test 3: late-window high-price scalp (pnl per $1 staked, %) ===")
SC_T = [(0, 5), (5, 15), (15, 30), (30, 60), (60, 120)]
SC_P = [(0.90, 0.95), (0.95, 0.97), (0.97, 0.99), (0.99, 1.0)]
for kind in ["5m", "15m", "1h", "4h"]:
    df = tt[tt.kind == kind]
    print(f"\n[{kind}] rows=tau(s), cols=price")
    hdr = "tau\\q    " + "".join(f"{f'{a}-{b}':>16s}" for a, b in SC_P)
    print(hdr)
    for tlo, thi in SC_T:
        line = f"{tlo:>3d}-{thi:<4d}"
        for plo, phi in SC_P:
            b = df[(df.tau >= tlo) & (df.tau < thi) & (df.q >= plo) & (df.q < phi)]
            if len(b) < 50 or b.slug.nunique() < 10:
                line += f"{'-':>16s}"
                continue
            m, lo, hi = boot_ci(b, col="ev1")
            star = "*" if (lo > 0 or hi < 0) else " "
            line += f"{100*m:>+9.2f}%{star} {b.slug.nunique():>3d}m"
        print(line)

# how often does the cheap side win late? (the "model dispute" scenario)
print("\n--- Test 3b: buying the CHEAP side late (q<0.10, tau<60) ---")
for kind in ["5m", "15m", "1h"]:
    b = tt[(tt.kind == kind) & (tt.q < 0.10) & (tt.tau < 60)]
    if len(b) < 50:
        continue
    m, lo, hi = boot_ci(b, col="ev1")
    print(f"[{kind}] n={len(b)} mkts={b.slug.nunique()} mean q={b.q.mean():.3f} "
          f"win%={100*np.average(b.y, weights=b['size']):.1f} "
          f"ev/$1={100*m:+.1f}% [{100*lo:+.1f},{100*hi:+.1f}]")
