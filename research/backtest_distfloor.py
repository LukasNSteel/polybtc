"""Does a LARGER distance-to-strike cushion cut the 'near-the-money late
reversion' losses (in-the-money most of the window, flips under in the final
seconds)? dist_sigma=0.5 implies ~31% revert-and-lose by construction; this
sweeps the floor over the 13-week archive, which settles on the ACTUAL window
close, so it captures exactly those last-second flips. Live config: 5m, UP-only.
"""
import numpy as np
import replay_binance as R


def stats(fs):
    if not fs:
        return (0, 0.0, 0.0, 0.0, 0.0)
    dep = sum(f[0] for f in fs); pnl = sum(f[2] for f in fs)
    return (len(fs), float(np.mean([f[3] for f in fs])), pnl,
            pnl / dep if dep else 0.0, R.drawdown(fs))


def main():
    print("loading...", flush=True)
    base, price, vol, obi = R.load_binance()
    ticks = R.load_ticks()
    live = dict(min_ask=0.50, max_ask=0.80, min_edge=0.10, max_edge=0.30,
                max_take_usd=10, max_position_usd=10, max_exposure_usd=60,
                cooldown=10, no_scale=True, kind_only="5m",
                min_t_rem=30, max_t_rem=90, trend_filter_sigma=1.0,
                race_loss=0.20, capture=0.30, contention=True, feedlag=True)
    for label, side_filter in [("UP-only 5m (matches live)", "up"),
                               ("BOTH sides 5m", None)]:
        print(f"\n=== {label} — distance floor sweep ===")
        hdr = f"  {'dist_sigma_min':14} {'fills':>6} {'win%':>6} {'pnl$':>8} {'ROI/$':>7} {'maxDD$':>7}"
        print(hdr); print("  " + "-" * (len(hdr) - 2))
        for floor in (0.5, 0.7, 0.9, 1.1, 1.3, 1.5):
            cfg = {**live, "dist_sigma_min": floor}
            fills, _ = R.run(cfg, base, price, vol, ticks, obi=obi)
            if side_filter:
                fills = [f for f in fills if f[5] == side_filter]
            n, w, p, roi, dd = stats(fills)
            print(f"  {floor:<14.1f} {n:>6} {w:>6.1%} {p:>+8.0f} {roi:>+7.1%} {dd:>7.0f}")


if __name__ == "__main__":
    main()
