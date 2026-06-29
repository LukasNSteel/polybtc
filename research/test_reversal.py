"""Does FAST repricing of the Polymarket book predict a REVERSAL or a CONTINUATION?

This directly tests the trader hypothesis: "when I see the book price rapidly
moving (e.g. making Down more favourable), market makers know something — it's
about to flip — so I should bet the OPPOSITE side and scalp out."

The fade thesis is true iff a fast move in p_up tends to REVERSE. We measure that
two ways on the live 5s calibration tape (logs/calibration.csv), which records the
book's own p_up over every market's life:

  TEST A — book-space markout (no settlement needed)
     trailing velocity  v = Δlogit(p_up) over the last ~`back` seconds
     forward move       f = Δlogit(p_up) over the next H seconds
     Regress f on v per kind & time-to-expiry band.
        slope < 0  -> mean reversion  (fast move snaps back -> FADE has signal)
        slope > 0  -> momentum        (fast move continues  -> fade is wrong-way)
        slope ~ 0  -> efficient / random walk (no edge either way)
     Plus the top-decile fastest moves: what is the mean forward move?

  TEST B — settlement +EV of the fade (the money question)
     When the book moves DOWN fast (p_up drops hard) we BUY UP at the current
     p_up and hold to settlement. EV/share = E[outcome_up] - p_up - taker_fee.
     (Hold-to-settle is the *fundamental* signal; "scalp out when green" only
      adds a second fee + variance, so if this is negative the scalp is worse.)
     Symmetric for fast-up -> buy DOWN. Reported net of the 0.07*p(1-p) fee.

Coarse caveat: the tape is 5s-cadence, so it under-samples the sub-second 250ms
repricing bursts the hypothesis is really about. A negative/zero result here is
strong (the effect isn't even visible at 5s); a positive result would justify
building the fine-grained 250ms observation logger. Run:  python research/test_reversal.py
"""
import sys
import numpy as np
import pandas as pd

PATH = sys.argv[1] if len(sys.argv) > 1 else "research/data/calibration_live.csv"
FEE = lambda p: 0.07 * p * (1 - p)  # noqa: E731  Polymarket taker fee per share


def logit(p):
    p = np.clip(p, 0.02, 0.98)
    return np.log(p / (1 - p))


def build(df, back=10.0, horizon=15.0, max_gap=8.0):
    """Per slug, attach trailing velocity (over ~`back`s) and forward move (over
    `horizon`s) in logit space, using each market's own time series."""
    out = []
    for slug, g in df.groupby("slug", sort=False):
        g = g.sort_values("ts")
        t = g.ts.values.astype(float)
        z = logit(g.p_up.values)
        n = len(t)
        if n < 4:
            continue
        # trailing index: last sample at or before t-back ; forward: first at >= t+horizon
        ib = np.searchsorted(t, t - back, side="right") - 1
        if_ = np.searchsorted(t, t + horizon, side="left")
        for i in range(n):
            jb, jf = ib[i], if_[i]
            if jb < 0 or jf >= n:
                continue
            # require the trailing/forward gaps to be real (not a long data hole)
            if (t[i] - t[jb]) > max_gap * 2 or (t[jf] - t[i]) > horizon + max_gap:
                continue
            out.append((slug, g.kind.iloc[i], g.t_remaining.iloc[i],
                        g.p_up.iloc[i], z[i] - z[jb], z[jf] - z[i]))
    return pd.DataFrame(out, columns=["slug", "kind", "tau", "p_up", "v", "f"])


def ols_slope(x, y):
    if len(x) < 30 or np.std(x) == 0:
        return float("nan"), float("nan"), len(x)
    b = np.polyfit(x, y, 1)
    r = np.corrcoef(x, y)[0, 1]
    return b[0], r, len(x)


def outcomes(df):
    """Per-slug settled Up outcome: explicit `outcome` if present, else infer from
    the last recorded p_up (it converges to 0/1 at expiry)."""
    res = {}
    for slug, g in df.groupby("slug", sort=False):
        g = g.sort_values("ts")
        oc = g.outcome.dropna()
        if len(oc):
            res[slug] = float(oc.iloc[-1])
        else:
            last = g.p_up.iloc[-1]
            res[slug] = 1.0 if last >= 0.5 else 0.0 if (last <= 0.05 or last >= 0.95) else np.nan
    return res


def main():
    df = pd.read_csv(PATH).dropna(subset=["p_up"])
    print(f"loaded {len(df):,} rows / {df.slug.nunique():,} markets / "
          f"{(df.ts.max()-df.ts.min())/3600:.0f}h\n")

    feat = build(df)
    tau_bands = [("early τ>300s", 300, 10**9), ("mid 60-300s", 60, 300),
                 ("late 15-60s", 15, 60)]

    print("=" * 78)
    print("TEST A — regress FORWARD move on TRAILING velocity (logit space)")
    print("  slope<0 = reversal (fade works) | >0 = momentum | ~0 = efficient")
    print("=" * 78)
    for kind in ["5m", "15m", "1h", "4h"]:
        print(f"\n[{kind}]")
        for name, lo, hi in tau_bands:
            g = feat[(feat.kind == kind) & (feat.tau > lo) & (feat.tau <= hi)
                     & (feat.p_up.between(0.05, 0.95))]
            slope, r, n = ols_slope(g.v.values, g.f.values)
            if n < 30:
                print(f"  {name:14} n={n:<6} (too few)")
                continue
            # top-decile fastest moves: do they revert? (mean forward move, signed
            # so that + = continuation in the move's own direction)
            q = g.v.abs().quantile(0.90)
            fast = g[g.v.abs() >= q]
            cont = float(np.mean(np.sign(fast.v) * fast.f))  # >0 continue, <0 revert
            verdict = "REVERSAL" if slope < -0.02 else "momentum" if slope > 0.02 else "efficient"
            print(f"  {name:14} n={n:<6} slope={slope:+.3f} r={r:+.3f}  "
                  f"top-decile fwd(in-dir)={cont:+.3f}  -> {verdict}")

    print("\n" + "=" * 78)
    print("TEST B — settlement +EV of FADING a fast move (hold to settle, net fee)")
    print("  fade = after fast DOWN move buy UP (and vice versa); EV/share in cents")
    print("=" * 78)
    oc = outcomes(df)
    feat["y"] = feat.slug.map(oc)
    fb = feat.dropna(subset=["y"])
    for kind in ["5m", "15m", "1h", "4h"]:
        print(f"\n[{kind}]")
        for name, lo, hi in tau_bands:
            g = fb[(fb.kind == kind) & (fb.tau > lo) & (fb.tau <= hi)
                   & (fb.p_up.between(0.10, 0.90))].copy()
            if len(g) < 50:
                print(f"  {name:14} n={len(g)} (too few)")
                continue
            thr = g.v.abs().quantile(0.85)  # the fast-repricing events
            for label, mask in (("FADE fast move", g.v.abs() >= thr),
                                 ("(all moves)", g.v.abs() >= 0)):
                e = g[mask]
                # fade: bet AGAINST the move's direction.
                #  move down (v<0) -> buy UP: pnl = y - p_up - fee
                #  move up   (v>0) -> buy DOWN: pnl = (1-y) - (1-p_up) - fee = p_up - y - fee
                buy_up = e.v < 0
                pnl = np.where(buy_up, e.y - e.p_up, e.p_up - e.y) - FEE(e.p_up)
                ev = 100 * np.mean(pnl)
                se = 100 * np.std(pnl) / np.sqrt(len(e))
                print(f"  {name:14} {label:16} n={len(e):<6} "
                      f"EV={ev:+.2f}c ± {1.96*se:.2f}c")
    print("\nNOTE 5s tape under-samples 250ms bursts; treat as a coarse first read.")


if __name__ == "__main__":
    main()
