"""Test a distance CEILING (dist_sigma_max) and an EDGE ceiling (max_edge) on the
13-week archive, on top of the live 5m config with the new 0.7 floor. Motivated
by 4 high-distance live losers (dσ 1.4-1.65 reverted) — but the floor sweep
showed win% RISING with distance, so the prior is that a ceiling HURTS. Settle
it with data: per-band win%/ROI first (does the top band actually lose?), then
ceiling sweeps.
"""
import numpy as np
import replay_binance as R


def stats(fs):
    if not fs:
        return (0, 0.0, 0.0, 0.0, 0.0)
    dep = sum(f[0] for f in fs); pnl = sum(f[2] for f in fs)
    return (len(fs), float(np.mean([f[3] for f in fs])), pnl,
            pnl / dep if dep else 0.0, R.drawdown(fs))


def row(lab, st):
    print(f"  {lab:18} {st[0]:>6} {st[1]:>6.1%} {st[2]:>+8.0f} {st[3]:>+7.1%} {st[4]:>7.0f}")


def main():
    print("loading...", flush=True)
    base, price, vol, obi = R.load_binance()
    ticks = R.load_ticks()
    live = dict(min_ask=0.50, max_ask=0.80, min_edge=0.10, max_edge=0.30,
                max_take_usd=10, max_position_usd=10, max_exposure_usd=60,
                cooldown=10, no_scale=True, kind_only="5m", dist_sigma_min=0.7,
                min_t_rem=30, max_t_rem=90, trend_filter_sigma=1.0,
                race_loss=0.20, capture=0.30, contention=True, feedlag=True)

    # need dist_sigma + edge per fill -> recompute features and index at fills.
    # easiest: run with floor 0.7, then re-derive each fill's dist_sigma/edge band
    # by re-running compute_features and matching. Instead, bucket via dedicated
    # floor/ceiling runs (set-difference gives each band's fills).
    print("\n=== DISTANCE bands (UP-only, floor stays >=0.7) — does the top band lose? ===")
    hdr = f"  {'band':18} {'fills':>6} {'win%':>6} {'pnl$':>8} {'ROI/$':>7} {'maxDD$':>7}"
    print(hdr)
    edges = [(0.7, 1.0), (1.0, 1.3), (1.3, 1.6), (1.6, 2.0), (2.0, 99)]
    for lo, hi in edges:
        cfg = {**live, "dist_sigma_min": lo, "dist_sigma_max": hi}
        f, _ = R.run(cfg, base, price, vol, ticks, obi=obi)
        f = [x for x in f if x[5] == "up"]
        row(f"dσ [{lo},{hi})", stats(f))

    print("\n=== DISTANCE CEILING sweep (UP-only, floor 0.7) ===")
    print(hdr)
    for cap in (1.0, 1.2, 1.5, 2.0, 99):
        cfg = {**live, "dist_sigma_max": cap}
        f, _ = R.run(cfg, base, price, vol, ticks, obi=obi)
        f = [x for x in f if x[5] == "up"]
        row(f"floor0.7 cap{cap}", stats(f))

    print("\n=== EDGE CEILING sweep (UP-only, floor 0.7) ===")
    print(hdr)
    for xe in (0.15, 0.20, 0.25, 0.30):
        cfg = {**live, "max_edge": xe}
        f, _ = R.run(cfg, base, price, vol, ticks, obi=obi)
        f = [x for x in f if x[5] == "up"]
        row(f"max_edge {xe}", stats(f))


if __name__ == "__main__":
    main()
