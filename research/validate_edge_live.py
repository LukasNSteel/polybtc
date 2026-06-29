"""Out-of-sample validation of the model-edge snipe on the FRESH 125h live tape.

test_taker_ev.py proved the edge on the June-12 study sample (executable taker
prices). This asks: does the *premise* still hold on the live data the bot just
generated — i.e. does the model's directional disagreement with the book's
(blended) fair still predict the outcome, net of fee?

calibration.csv logs, every 5s: p_up = the bot's BLENDED fair (model blended
toward the book mid) and model_p = the RAW model. We take the MODEL-FAVOURED
side on the FAVOURITE band [0.50,0.80] and hold to settlement:

    side      = up if model_p > p_up else down
    entry q   = p_up (up) or 1-p_up (down)          <- the blended fair, NOT an ask
    edge      = |model_p - p_up| - fee(q)
    pnl/share = y_side - q - fee(q)

Entry at the *fair* (no spread, no lag discount) is CONSERVATIVE vs the real
snipe, which buys a lagging ask BELOW fair — so a positive result here is a
lower bound on the live snipe edge; a flat/negative result means the model's
residual signal beyond the book has decayed. We bucket by edge and by time-to-
expiry (the snipe lives at low tau, where the book lags Binance) and cluster-
bootstrap CIs by market. Also reports the realistic "one best entry per market".

Run: python research/validate_edge_live.py
"""
import sys
import numpy as np
import pandas as pd

PATH = sys.argv[1] if len(sys.argv) > 1 else "research/data/calibration_live.csv"
rng = np.random.default_rng(7)
fee = lambda q: 0.07 * q * (1 - q)  # noqa: E731


def boot_ci(pnl, slug, n=400):
    d = pd.DataFrame({"x": pnl, "slug": slug})
    a = d.groupby("slug").agg(s=("x", "sum"), c=("x", "size"))
    s, c = a.s.values, a.c.values
    point = s.sum() / c.sum()
    k = len(s)
    idx = rng.integers(0, k, size=(n, k))
    means = s[idx].sum(1) / c[idx].sum(1)
    lo, hi = np.percentile(means, [2.5, 97.5])
    return point, lo, hi


def per_slug_outcome(df):
    res = {}
    for slug, g in df.groupby("slug", sort=False):
        g = g.sort_values("ts")
        oc = g.outcome.dropna()
        if len(oc):
            res[slug] = float(oc.iloc[-1]); continue
        last = g.loc[g.t_remaining.idxmin(), "p_up"]
        res[slug] = 1.0 if last >= 0.98 else 0.0 if last <= 0.02 else np.nan
    return res


def main():
    df = pd.read_csv(PATH).dropna(subset=["p_up", "model_p"])
    y = per_slug_outcome(df)
    df = df[df.t_remaining > 0].copy()
    df["y"] = df.slug.map(y)
    df = df.dropna(subset=["y"])

    # take the model-favoured side; entry at the blended fair
    up = df.model_p > df.p_up
    df["q"] = np.where(up, df.p_up, 1 - df.p_up)
    df["y_side"] = np.where(up, df.y, 1 - df.y)
    df["edge"] = (df.model_p - df.p_up).abs() - fee(df.q)
    df["pnl"] = df.y_side - df.q - fee(df.q)
    # favourite-band gate, exactly like the live sniper (min_ask..max_ask)
    fav = df[(df.q >= 0.50) & (df.q <= 0.80)].copy()
    print(f"loaded {len(df):,} samples / {df.slug.nunique()} resolved markets; "
          f"favourite-band samples {len(fav):,}\n")

    EDGES = [(-1, 0.0), (0.0, 0.03), (0.03, 0.08), (0.08, 0.15), (0.15, 0.25), (0.25, 1.0)]
    TAUS = {"all τ": (0, 10**9), "τ≤60s": (0, 60), "τ 60-300": (60, 300)}

    print("=" * 80)
    print("MODEL-EDGE SNIPE on fresh data — favourite band, hold to settle, net fee")
    print("  pnl/share in cents; * = 95% market-cluster CI excludes 0")
    print("=" * 80)
    for kind in ["5m", "15m", "1h", "4h"]:
        print(f"\n[{kind}]")
        for tname, (tlo, thi) in TAUS.items():
            sub = fav[(fav.kind == kind) & (fav.t_remaining > tlo) & (fav.t_remaining <= thi)]
            if len(sub) < 200:
                continue
            print(f"  --- {tname} ---")
            for lo, hi in EDGES:
                b = sub[(sub.edge >= lo) & (sub.edge < hi)]
                if len(b) < 100 or b.slug.nunique() < 8:
                    continue
                m, l, h = boot_ci(b.pnl.values, b.slug.values)
                star = "*" if (l > 0 or h < 0) else " "
                print(f"    edge {lo:+.2f}..{hi:+.2f}: n={len(b):6d} mkts={b.slug.nunique():4d} "
                      f"q={b.q.mean():.2f} pnl/sh={100*m:+.2f}c [{100*l:+.2f},{100*h:+.2f}]{star}")

    # realistic: ONE best entry per market (max edge in favourite band), hold to settle
    print("\n" + "=" * 80)
    print("ONE BEST ENTRY PER MARKET (max-edge favourite-band sample, edge>=0.03)")
    print("=" * 80)
    cand = fav[fav.edge >= 0.03]
    for kind in ["5m", "15m", "1h", "4h"]:
        g = cand[cand.kind == kind]
        if not len(g):
            continue
        best = g.loc[g.groupby("slug").edge.idxmax()]
        if len(best) < 15:
            print(f"  [{kind}] n={len(best)} (too few)"); continue
        m, l, h = boot_ci(best.pnl.values, best.slug.values)
        win = 100 * np.mean(best.pnl.values > 0)
        star = "*" if (l > 0 or h < 0) else " "
        print(f"  [{kind}] mkts={len(best):4d} mean q={best.q.mean():.2f} "
              f"win%={win:.0f} pnl/sh={100*m:+.2f}c [{100*l:+.2f},{100*h:+.2f}]{star}")
    print("\nReminder: entry is at the FAIR (no spread/lag discount) so this is a")
    print("conservative lower bound on the executable snipe; the live leg buys a")
    print("lagging ask below fair. Flat here at high tau = book efficient at rest.")


if __name__ == "__main__":
    main()
