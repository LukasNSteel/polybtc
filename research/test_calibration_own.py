"""Analyze the bot's own calibration.csv: is model_p well calibrated vs outcomes,
and does it beat the blended/market price?"""

import pandas as pd
import numpy as np

df = pd.read_csv("logs/calibration.csv")
print(f"rows: {len(df)}")

# outcomes appear on settle rows; propagate to all rows of that slug
out = df.dropna(subset=["outcome"]).groupby("slug")["outcome"].last()
df["y"] = df["slug"].map(out)
df = df.dropna(subset=["y", "model_p", "p_up"]).copy()
df["y"] = df["y"].astype(float)
print(f"rows with outcome: {len(df)}, markets: {df['slug'].nunique()}")
print(df.groupby("kind")["slug"].nunique())

def brier(p, y):
    return float(np.mean((p - y) ** 2))

def logloss(p, y):
    p = np.clip(p, 1e-4, 1 - 1e-4)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))

# bucket by time remaining
buckets = [(0, 30), (30, 60), (60, 120), (120, 300), (300, 900), (900, 3600), (3600, 1e9)]
rows = []
for kind, g in df.groupby("kind"):
    for lo, hi in buckets:
        b = g[(g.t_remaining >= lo) & (g.t_remaining < hi)]
        if len(b) < 50:
            continue
        rows.append({
            "kind": kind, "t_rem": f"{lo}-{int(min(hi,99999))}", "n": len(b),
            "brier_model": round(brier(b.model_p, b.y), 4),
            "brier_blend": round(brier(b.p_up, b.y), 4),
            "ll_model": round(logloss(b.model_p, b.y), 4),
            "ll_blend": round(logloss(b.p_up, b.y), 4),
        })
r = pd.DataFrame(rows)
print("\n=== Brier / logloss by kind and time remaining (lower=better) ===")
print(r.to_string(index=False))

# calibration curve for model_p
print("\n=== Calibration: model_p deciles -> empirical P(up) ===")
df["bin"] = (df.model_p * 10).clip(0, 9).astype(int)
cal = df.groupby(["kind", "bin"]).agg(n=("y", "size"), pred=("model_p", "mean"), emp=("y", "mean"))
cal["gap"] = (cal.emp - cal.pred).round(3)
print(cal[cal.n >= 100].round(3).to_string())

# does model_p disagreement with blend predict outcome? (proxy for edge signal)
df["edge"] = df.model_p - df.p_up
big = df[abs(df.edge) > 0.03]
if len(big):
    # when model > market, did UP happen more than market implied?
    print("\n=== When |model - blend| > 3c: who was right? ===")
    for kind, g in big.groupby("kind"):
        up_bias = g[g.edge > 0.03]
        dn_bias = g[g.edge < -0.03]
        for name, gg in [("model>mkt", up_bias), ("model<mkt", dn_bias)]:
            if len(gg) < 30:
                continue
            print(f"{kind:4s} {name}: n={len(gg):5d}  mkt_implied={gg.p_up.mean():.3f}  "
                  f"model={gg.model_p.mean():.3f}  empirical={gg.y.mean():.3f}")
