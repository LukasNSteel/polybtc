"""Tests 4-5: signal value beyond price, and flow toxicity.

Test 4: on snapshot grid, does anything beat the market price as a predictor?
        Features: logit(market p), model dist_z, momentum z, flow.
        Walk-forward by time (train on first 60% of windows, test on last 40%).
Test 5: markouts after taker executions (adverse selection felt by makers),
        and whether following recent taker flow is +EV.
"""

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression

D = "research/data"
rng = np.random.default_rng(11)

sn = pd.read_parquet(f"{D}/snapshots.parquet")
tt = pd.read_parquet(f"{D}/taker_trades.parquet")


def logit(p):
    p = np.clip(p, 0.005, 0.995)
    return np.log(p / (1 - p))


def logloss(y, p):
    p = np.clip(p, 1e-4, 1 - 1e-4)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


print("=== Test 4: predictive value beyond market price (walk-forward) ===")
sn = sn.dropna(subset=["p_up", "model_p_up"]).copy()
sn["x_mkt"] = logit(sn.p_up)
sn["x_model"] = logit(sn.model_p_up)
sn["x_mom"] = np.clip(sn.mom_z, -3, 3)
sn["x_flow"] = np.sign(sn.flow60.fillna(0)) * np.log1p(np.abs(sn.flow60.fillna(0)))

for kind in ["5m", "15m", "1h", "4h"]:
    g = sn[sn.kind == kind].copy()
    if g.slug.nunique() < 40:
        print(f"[{kind}] too few markets")
        continue
    # split by window start time so test is strictly out-of-sample in time
    order = sorted(g.slug.unique())
    cut = order[int(len(order) * 0.6)]
    tr_, te_ = g[g.slug <= cut], g[g.slug > cut]
    y_tr, y_te = tr_.y_up.values, te_.y_up.values

    specs = {
        "mkt only": ["x_mkt"],
        "mkt+model": ["x_mkt", "x_model"],
        "mkt+mom": ["x_mkt", "x_mom"],
        "mkt+flow": ["x_mkt", "x_flow"],
        "mkt+model+mom+flow": ["x_mkt", "x_model", "x_mom", "x_flow"],
    }
    print(f"\n[{kind}] train {tr_.slug.nunique()} mkts / test {te_.slug.nunique()} mkts "
          f"({len(te_)} snaps)  baseline raw-price logloss={logloss(y_te, te_.p_up.values):.4f}")
    for name, cols in specs.items():
        m = LogisticRegression(C=10.0, max_iter=1000)
        m.fit(tr_[cols], y_tr)
        p = m.predict_proba(te_[cols])[:, 1]
        coefs = " ".join(f"{c}={v:+.2f}" for c, v in zip(cols, m.coef_[0]))
        print(f"  {name:22s} test logloss={logloss(y_te, p):.4f}   {coefs}")

print("\n\n=== Test 5a: post-trade markouts (does taker flow predict price?) ===")
tt["dir"] = np.where(tt.bought_up, 1.0, -1.0)
for kind in ["5m", "15m", "1h", "4h"]:
    g = tt[(tt.kind == kind) & tt.p_up_10s.notna()]
    if len(g) < 500:
        continue
    mo10 = ((g.p_up_10s - g.p_up_trade) * g.dir)
    g6 = g[g.p_up_60s.notna() & (g.tau > 60)]
    mo60 = ((g6.p_up_60s - g6.p_up_trade) * g6.dir)
    # weighted by notional = what a maker actually faces
    w10 = np.average(mo10, weights=g.notional)
    w60 = np.average(mo60, weights=g6.notional) if len(g6) else float("nan")
    print(f"[{kind}] markout10s={100*w10:+.2f}c  markout60s={100*w60:+.2f}c "
          f"(n={len(g)}, notional-weighted, in taker direction)")

print("\n=== Test 5b: trade WITH recent flow — EV of following 60s flow ===")
for kind in ["5m", "15m", "1h", "4h"]:
    g = tt[(tt.kind == kind) & tt.flow60.notna() & (tt.q >= 0.05) & (tt.q <= 0.95)].copy()
    # signed flow BEFORE this trade (exclude own print)
    g["flow_pre"] = g.flow60 - np.where(g.bought_up, g.notional, -g.notional)
    g["aligned"] = np.sign(g.flow_pre) == np.where(g.bought_up, 1, -1)
    for thr in [200, 1000]:
        a = g[g.flow_pre.abs() > thr]
        if len(a) < 300:
            continue
        wi = a[a.aligned]
        ag = a[~a.aligned]
        pw = np.average(wi.y - wi.q - wi.fee, weights=wi["size"]) if len(wi) else np.nan
        pa = np.average(ag.y - ag.q - ag.fee, weights=ag["size"]) if len(ag) else np.nan
        print(f"[{kind}] |flow60|>{thr:>5d}: WITH flow n={len(wi):6d} pnl={100*pw:+.2f}c/sh | "
              f"AGAINST n={len(ag):6d} pnl={100*pa:+.2f}c/sh")
