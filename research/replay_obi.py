"""Does Binance order-FLOW imbalance (OFI/aggressor imbalance) add signal to the
sniper, on top of the existing price lead-lag (recent-return drift)?

Reuses the faithful replay engine in replay_binance.py (same model, same paper
frictions: race-loss + capture + edge_contention + feed_lag). The flow imbalance
is built from the 1s kline taker-buy volume (col 9) vs total volume (col 5):
    ofi_raw = (2*taker_buy - volume) / volume   in [-1, 1]
then EWMA-smoothed (halflife ~20s). We test three uses:
  1. DIAGNOSTIC  - among the baseline fills, does sign(OFI) predict the winner?
  2. GATE        - only snipe a side when flow agrees (ob >= thr / <= -thr).
  3. TILT        - fold OFI into the drift + robust bounds (combine with lead-lag).

NOTE: this is order-FLOW imbalance, not L2 book imbalance — true top-of-book OBI
needs Binance depth snapshots we did not scrape. For sub-minute horizons the
aggressor-flow imbalance is the stronger, more standard lead signal anyway.
"""
import numpy as np
import pandas as pd

from replay_binance import load_binance, load_ticks, run, drawdown


def row(name, fills):
    if not fills:
        print(f"{name:46} {'0':>6}")
        return
    dep = sum(f[0] for f in fills)
    pnl = sum(f[2] for f in fills)
    win = np.mean([f[3] for f in fills])
    mdd = drawdown(fills)
    roi = pnl / dep if dep else 0.0
    pdd = pnl / mdd if mdd else 0.0
    print(f"{name:46} {len(fills):>6} {win:>6.1%} {dep:>9.0f} {pnl:>+8.0f} "
          f"{roi:>+7.1%} {mdd:>7.0f} {pdd:>7.1f}")


def diagnostic(fills):
    """Among baseline fills, split by whether the flow agreed with the bet side."""
    agree = [f for f in fills if (f[7] > 0) == (f[5] == "up") and abs(f[7]) > 1e-6]
    disag = [f for f in fills if (f[7] > 0) != (f[5] == "up") and abs(f[7]) > 1e-6]
    print("\n--- OFI predictiveness on the baseline OPT.20 fills ---")
    print(f"{'flow vs bet side':22} {'fills':>6} {'win%':>7} {'pnl$':>9} {'ROI/$':>8}")
    for label, grp in (("flow AGREES", agree), ("flow DISAGREES", disag)):
        if not grp:
            print(f"{label:22} {'0':>6}")
            continue
        dep = sum(f[0] for f in grp); pnl = sum(f[2] for f in grp)
        win = np.mean([f[3] for f in grp])
        print(f"{label:22} {len(grp):>6} {win:>7.1%} {pnl:>+9.0f} {pnl/dep:>+8.1%}")
    obs = np.array([f[7] for f in fills])
    wins = np.array([f[3] for f in fills])
    side_up = np.array([1.0 if f[5] == "up" else -1.0 for f in fills])
    # correlation of signed flow with realized win/loss of the chosen side
    signed = obs * side_up
    if signed.std() > 0:
        print(f"corr(signed OFI, win) = {np.corrcoef(signed, wins)[0,1]:+.4f}  "
              f"(n={len(fills)})")


def main():
    print("loading binance 1s klines + flow imbalance...", flush=True)
    base, price, vol, obi = load_binance(obi_halflife=20.0)
    print(f"  coverage {len(price):,}s; OFI mean {obi.mean():+.4f} std {obi.std():.4f}")
    print("loading parquet book + outcomes...", flush=True)
    ticks = load_ticks()
    print(f"  ticks: {len(ticks['t']):,}\n")

    base_cfg = dict(min_ask=0.50, max_ask=0.80, min_edge=0.10, max_edge=0.20,
                    max_take_usd=30, max_position_usd=60, max_exposure_usd=200,
                    cooldown=10, race_loss=0.20, capture=0.30,
                    contention=True, feedlag=True, no_scale=True)

    # baseline once, with the diagnostic
    base_fills, _ = run(base_cfg, base, price, vol, ticks, obi=obi)
    print(f"{'scenario (OPT.20, full frictions)':46} {'fills':>6} {'win%':>6} "
          f"{'dep$':>9} {'pnl$':>8} {'ROI/$':>7} {'maxDD$':>7} {'pnl/DD':>7}")
    print("-" * 100)
    row("baseline (no OFI)", base_fills)

    scenarios = {
        # GATE: require flow to agree in sign (thr=0), then stronger thresholds
        "+ OFI gate >=0.00 (sign agree)": dict(obi_gate=0.0),
        "+ OFI gate >=0.05": dict(obi_gate=0.05),
        "+ OFI gate >=0.10": dict(obi_gate=0.10),
        "+ OFI gate >=0.20": dict(obi_gate=0.20),
        # TILT: fold OFI into drift + bounds (combine with lead-lag)
        "+ OFI tilt 0.5": dict(obi_tilt=0.5),
        "+ OFI tilt 1.0": dict(obi_tilt=1.0),
        "+ OFI tilt 2.0": dict(obi_tilt=2.0),
        # combined
        "+ OFI tilt 1.0 & gate >=0.00": dict(obi_tilt=1.0, obi_gate=0.0),
        "+ OFI tilt 1.0 & gate >=0.10": dict(obi_tilt=1.0, obi_gate=0.10),
    }
    for name, override in scenarios.items():
        fills, _ = run({**base_cfg, **override}, base, price, vol, ticks, obi=obi)
        row(name, fills)

    diagnostic(base_fills)
    print("\nFixed caps, $1000 start, no equity-scaling. OFI = Binance aggressor "
          "flow imbalance (EWMA hl=20s).")


if __name__ == "__main__":
    main()
