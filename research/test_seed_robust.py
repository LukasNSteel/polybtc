"""Multi-seed robustness of the candidate $1000 caps: does the 2-month drawdown
stay UNDER the $1000 bankroll across many random fill-race seeds, or did exp175
just get a lucky path? Reports PnL and maxDD distribution per cap level.

Run:  PYTHONPATH=research python research/test_seed_robust.py
"""
import numpy as np

from replay_binance import load_binance, load_ticks, run, drawdown

CAPITAL = 1000.0
SHARED = dict(min_ask=0.50, max_ask=0.80, min_edge=0.10, max_edge=0.20,
              cooldown=10, race_loss=0.20, capture=0.30,
              contention=True, feedlag=True, no_scale=True)
LEVELS = [("exp200", 32, 64, 200), ("exp300", 48, 96, 300),
          ("exp400", 64, 128, 400), ("exp600", 96, 192, 600)]
SEEDS = list(range(1, 13))


def main():
    print("loading data...", flush=True)
    base, price, vol, obi = load_binance()
    ticks = load_ticks()
    print(f"  ticks: {len(ticks['t']):,}, seeds: {len(SEEDS)}\n")

    print(f"{'caps':10} {'pnl med':>9} {'pnl min':>9} {'pnl max':>9} "
          f"{'DD med':>8} {'DD max':>8} {'DD%cap max':>10} {'survive all?':>13}")
    print("-" * 90)
    for label, take, pos, exp in LEVELS:
        pnls, dds = [], []
        for sd in SEEDS:
            cfg = {**SHARED, "max_take_usd": take, "max_position_usd": pos,
                   "max_exposure_usd": exp, "seed": sd}
            fills, _ = run(cfg, base, price, vol, ticks, obi=obi)
            pnls.append(sum(f[2] for f in fills))
            dds.append(drawdown(fills))
        pnls, dds = np.array(pnls), np.array(dds)
        survive = "YES" if dds.max() < CAPITAL else f"NO ({(dds>=CAPITAL).sum()}/{len(SEEDS)})"
        print(f"{label:10} {np.median(pnls):>+9.0f} {pnls.min():>+9.0f} {pnls.max():>+9.0f} "
              f"{np.median(dds):>8.0f} {dds.max():>8.0f} {dds.max()/CAPITAL:>9.1%} {survive:>13}")
    print("-" * 90)
    print(f"{len(SEEDS)} seeds, $1000 bankroll, full window. 'survive all?' = maxDD<$1000")
    print("on EVERY seed (the conservative survival test).")


if __name__ == "__main__":
    main()
