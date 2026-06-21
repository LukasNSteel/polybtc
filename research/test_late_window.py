"""Experiment: does restricting the sniper to the last N seconds of a market
help or hurt?

Reuses the faithful replay engine in replay_binance.py (same model, same paper
frictions: race-loss + capture + edge-contention + feed-lag). Everything is held
identical across runs except the time-to-resolution gate.

Three views:
  1. CUMULATIVE "last N s only": trade only when t_rem <= N (down to settlement).
  2. EXCLUSIVE time bands: which slice of the window actually makes the money.
  3. Same as (1) but with a 5s settlement floor (min_t_rem=5) to avoid the
     degenerate final ticks where the model blows up (vol*sqrt(t_rem)->0).

Run:  python research/test_late_window.py
"""
import numpy as np

from replay_binance import load_binance, load_ticks, run, drawdown


# Same fixed caps + full friction model as replay_binance.main()'s base_cfg, so
# numbers here line up with the canonical backtester. No equity compounding.
BASE = dict(min_ask=0.50, max_ask=0.80, min_edge=0.10, max_edge=0.20,
            max_take_usd=30, max_position_usd=60, max_exposure_usd=200,
            cooldown=10, race_loss=0.20, capture=0.30,
            contention=True, feedlag=True, no_scale=True)


def summarize(name, cfg, data):
    base, price, vol, ticks, obi = data
    fills, _ = run(cfg, base, price, vol, ticks, obi=obi)
    if not fills:
        print(f"{name:42} {'0':>6}")
        return
    dep = sum(f[0] for f in fills)
    pnl = sum(f[2] for f in fills)
    win = np.mean([f[3] for f in fills])
    mdd = drawdown(fills)
    print(f"{name:42} {len(fills):>6} {win:>6.1%} {dep:>9.0f} {pnl:>+8.0f} "
          f"{pnl/dep:>+7.2%} {mdd:>7.0f} {pnl/mdd if mdd else 0:>7.1f}")


def header():
    print(f"{'scenario':42} {'fills':>6} {'win%':>6} {'dep$':>9} {'pnl$':>8} "
          f"{'ROI/$':>7} {'maxDD$':>7} {'pnl/DD':>7}")
    print("-" * 95)


def main():
    print("loading binance 1s klines...", flush=True)
    base, price, vol, obi = load_binance()
    print("loading parquet book + outcomes...", flush=True)
    ticks = load_ticks()
    print(f"  ticks: {len(ticks['t']):,}\n")
    data = (base, price, vol, ticks, obi)

    print("=== 1) CUMULATIVE: trade only in the LAST N seconds (down to close) ===")
    header()
    summarize("baseline: full window (no time gate)", {**BASE}, data)
    summarize("last 90s only (t_rem <= 90)", {**BASE, "max_t_rem": 90}, data)
    summarize("last 60s only (t_rem <= 60)", {**BASE, "max_t_rem": 60}, data)
    summarize("last 30s only (t_rem <= 30)", {**BASE, "max_t_rem": 30}, data)

    print("\n=== 2) EXCLUSIVE bands: where does the PnL actually come from? ===")
    header()
    summarize("early: t_rem > 90", {**BASE, "min_t_rem": 90}, data)
    summarize("band (60, 90]", {**BASE, "min_t_rem": 60, "max_t_rem": 90}, data)
    summarize("band (30, 60]", {**BASE, "min_t_rem": 30, "max_t_rem": 60}, data)
    summarize("band (0, 30]", {**BASE, "max_t_rem": 30}, data)

    print("\n=== 3) CUMULATIVE last-N with a 5s settlement floor (t_rem in [5,N]) ===")
    print("    (avoids the degenerate final ticks where the model is unstable)")
    header()
    summarize("last 90s, floor 5s [5,90]", {**BASE, "min_t_rem": 5, "max_t_rem": 90}, data)
    summarize("last 60s, floor 5s [5,60]", {**BASE, "min_t_rem": 5, "max_t_rem": 60}, data)
    summarize("last 30s, floor 5s [5,30]", {**BASE, "min_t_rem": 5, "max_t_rem": 30}, data)
    print("-" * 95)
    print("Fixed caps, $1000 start, no compounding. ROI/$ = pnl per $ deployed")
    print("(friction-robust efficiency). ~8 weeks of 5m/15m/1h/4h markets.")


if __name__ == "__main__":
    main()
