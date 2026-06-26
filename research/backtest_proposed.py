"""Backtest the PROPOSED sniper rule set against the CURRENT live rules on the
full ~13-week BTC parquet archive (data.binance.vision 1s klines + Polymarket
book ticks + ground-truth settlements). This is the largest out-of-sample set we
have (vs the 47 live fills the recommendation was derived from), with the full
paper fill-realism (race-loss + capture + edge_contention + feed_lag).

Rules under test (5m markets — the leg we actually trade and tuned on):
  * favourite band      min_ask 0.50  -> max_ask 0.65   (the proposed change)
  * distance-to-strike  dist_sigma_min 0.50             (deployed, kept)
  * edge band           net_edge in (0.10, 0.30]        (kept)
  * timing window       t_remaining in [30, 90]s        (deployed, kept)
  * trend filter        don't fade momentum             (deployed 1.5; test 1.0/0.5/0)

We isolate each lever so we can see what the max_ask change buys on its own,
and split UP vs DN to check the DN bleed flagged in the live forensics.
"""
import numpy as np
import replay_binance as R


def stats(fills):
    if not fills:
        return dict(n=0, win=0.0, dep=0.0, pnl=0.0, roi=0.0, mdd=0.0)
    dep = sum(f[0] for f in fills)
    pnl = sum(f[2] for f in fills)
    win = float(np.mean([f[3] for f in fills]))
    mdd = R.drawdown(fills)
    return dict(n=len(fills), win=win, dep=dep, pnl=pnl,
                roi=pnl / dep if dep else 0.0, mdd=mdd)


def side_split(fills):
    out = {}
    for sd in ("up", "dn"):
        sub = [f for f in fills if f[5] == sd]
        out[sd] = stats(sub)
    return out


def main():
    print("loading binance 1s klines (~13wk)...", flush=True)
    base, price, vol, obi = R.load_binance()
    print("loading parquet book + outcomes...", flush=True)
    ticks = R.load_ticks()

    # Fixed caps, no equity compounding -> $ and ROI/$ are comparable across runs.
    # ROI/$ deployed is the sizing-robust efficiency metric; win% is sizing-free.
    base_cfg = dict(min_ask=0.50, max_ask=0.80, min_edge=0.10, max_edge=0.30,
                    max_take_usd=10, max_position_usd=10, max_exposure_usd=60,
                    cooldown=10, no_scale=True,
                    kind_only="5m", dist_sigma_min=0.50,
                    min_t_rem=30, max_t_rem=90)

    # friction regimes: realistic (engine default) and a harsher live-calibrated
    # one (race ~0.72 from the observed live miss-rate, thin capture).
    regimes = {
        "REALISTIC (race .20 / cap .30 / contention+feedlag)":
            dict(race_loss=0.20, capture=0.30, contention=True, feedlag=True),
        "HARSH live-calibrated (race .72 / cap .10)":
            dict(race_loss=0.72, capture=0.10, contention=True, feedlag=True),
    }

    scenarios = {
        "raw favourite-only [.50,.80], no dist/trend/window":
            dict(max_ask=0.80, dist_sigma_min=0.0, min_t_rem=0, max_t_rem=0),
        "+ dist .50 + window [30,90] (no trend, max_ask .80)":
            dict(max_ask=0.80),
        "CURRENT live (max_ask .80, trend 1.5)":
            dict(max_ask=0.80, trend_filter_sigma=1.5),
        "isolate max_ask .65 (trend 1.5)":
            dict(max_ask=0.65, trend_filter_sigma=1.5),
        "PROPOSED (max_ask .65, trend 1.0)":
            dict(max_ask=0.65, trend_filter_sigma=1.0),
        "PROPOSED trend 0.5":
            dict(max_ask=0.65, trend_filter_sigma=0.5),
        "PROPOSED no-fade (trend 0.0)":
            dict(max_ask=0.65, trend_filter_sigma=0.0),
    }

    for rname, rcfg in regimes.items():
        print(f"\n=== {rname} ===")
        hdr = f"{'scenario':52} {'fills':>5} {'win%':>6} {'dep$':>8} {'pnl$':>8} {'ROI/$':>7} {'maxDD$':>7}"
        print(hdr); print("-" * len(hdr))
        keep = None
        for name, override in scenarios.items():
            cfg = {**base_cfg, **rcfg, **override}
            fills, _ = R.run(cfg, base, price, vol, ticks, obi=obi)
            st = stats(fills)
            print(f"{name:52} {st['n']:>5} {st['win']:>6.1%} {st['dep']:>8.0f} "
                  f"{st['pnl']:>+8.0f} {st['roi']:>+7.1%} {st['mdd']:>7.0f}")
            if name.startswith("PROPOSED (max_ask"):
                keep = fills
        # UP vs DN split for the headline proposed rule
        if keep:
            sp = side_split(keep)
            print("  PROPOSED side split:")
            for sd in ("up", "dn"):
                s = sp[sd]
                print(f"    {sd.upper():3} {s['n']:>5} fills  win {s['win']:>5.1%}  "
                      f"pnl {s['pnl']:>+7.0f}  ROI/$ {s['roi']:>+6.1%}")


if __name__ == "__main__":
    main()
