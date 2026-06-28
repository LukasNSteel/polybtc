"""Out-of-sample check of the SIDE GATE (snipe_sides=[up]) on the full ~13-week
BTC parquet archive, under the ACTUAL current live rules (max_ask 0.80, trend
1.0, dist 0.5, window [30,90]s, 5m). The live forensics (49 fills) said DN is
-EV in every sub-bucket; this confirms whether that holds over the largest
out-of-sample set we have, so the pause isn't a 49-fill / bull-week overfit.
"""
import numpy as np
import replay_binance as R


def stats(fills):
    if not fills:
        return dict(n=0, win=0.0, dep=0.0, pnl=0.0, roi=0.0, mdd=0.0)
    dep = sum(f[0] for f in fills); pnl = sum(f[2] for f in fills)
    return dict(n=len(fills), win=float(np.mean([f[3] for f in fills])),
                dep=dep, pnl=pnl, roi=pnl / dep if dep else 0.0,
                mdd=R.drawdown(fills))


def row(name, st):
    print(f"{name:34} {st['n']:>5} {st['win']:>6.1%} {st['dep']:>8.0f} "
          f"{st['pnl']:>+8.0f} {st['roi']:>+7.1%} {st['mdd']:>7.0f}")


def main():
    print("loading binance 1s klines (~13wk)...", flush=True)
    base, price, vol, obi = R.load_binance()
    print("loading parquet book + outcomes...", flush=True)
    ticks = R.load_ticks()

    live = dict(min_ask=0.50, max_ask=0.80, min_edge=0.10, max_edge=0.30,
                max_take_usd=10, max_position_usd=10, max_exposure_usd=60,
                cooldown=10, no_scale=True, kind_only="5m", dist_sigma_min=0.50,
                min_t_rem=30, max_t_rem=90, trend_filter_sigma=1.0)

    regimes = {
        "REALISTIC (race .20 / cap .30)":
            dict(race_loss=0.20, capture=0.30, contention=True, feedlag=True),
        "HARSH live-cal (race .72 / cap .10)":
            dict(race_loss=0.72, capture=0.10, contention=True, feedlag=True),
    }
    for rname, rcfg in regimes.items():
        print(f"\n=== {rname} ===")
        hdr = f"{'config':34} {'fills':>5} {'win%':>6} {'dep$':>8} {'pnl$':>8} {'ROI/$':>7} {'maxDD$':>7}"
        print(hdr); print("-" * len(hdr))
        fills, _ = R.run({**live, **rcfg}, base, price, vol, ticks, obi=obi)
        row("BOTH sides (current)", stats(fills))
        row("  UP only", stats([f for f in fills if f[5] == "up"]))
        row("  DN only", stats([f for f in fills if f[5] == "dn"]))
        print("  -- trend_filter sweep (both sides) --")
        for ts in (1.5, 1.0, 0.5, 0.0):
            f2, _ = R.run({**live, **rcfg, "trend_filter_sigma": ts},
                          base, price, vol, ticks, obi=obi)
            row(f"  trend {ts}", stats(f2))


if __name__ == "__main__":
    main()
