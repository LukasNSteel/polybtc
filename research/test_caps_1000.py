"""On $1000 of real capital, what sizing caps maximize 2-month PnL while keeping
the peak-to-trough drawdown UNDER the bankroll (so you never go broke and never
trip the kill switch en route to the winners)?

Same faithful replay engine + full frictions as replay_binance.py. Full window
(no time gate) -- the point is to find the largest caps you can SURVIVE, since
survival (not the time gate) is what unlocks the early-window PnL.

Exposure is the primary risk lever; take/pos scale with it at the $250-tier
ratio (take = 0.16*exp, pos = 0.32*exp). $1000 bankroll throughout.

Run:  PYTHONPATH=research python research/test_caps_1000.py
"""
import numpy as np

from replay_binance import load_binance, load_ticks, run, drawdown

CAPITAL = 1000.0
SHARED = dict(min_ask=0.50, max_ask=0.80, min_edge=0.10, max_edge=0.20,
              cooldown=10, race_loss=0.20, capture=0.30,
              contention=True, feedlag=True, no_scale=True)

# (label, exposure cap). take/pos derived at the $250-tier ratio.
EXP_LEVELS = [
    ("$250 caps (exp125)", 125),
    ("exp150", 150),
    ("exp175", 175),
    ("$1000 caps* (exp200)", 200),   # *uses the literal $1k-tier 30/60 caps
    ("exp250", 250),
    ("exp300", 300),
]


def caps_for(exp, exact=None):
    if exact:
        return dict(max_take_usd=exact[0], max_position_usd=exact[1],
                    max_exposure_usd=exp)
    return dict(max_take_usd=round(0.16 * exp), max_position_usd=round(0.32 * exp),
                max_exposure_usd=exp)


def summarize(name, cfg, data):
    base, price, vol, ticks, obi = data
    fills, _ = run(cfg, base, price, vol, ticks, obi=obi)
    if not fills:
        print(f"{name:24} {'0':>6}")
        return
    dep = sum(f[0] for f in fills)
    pnl = sum(f[2] for f in fills)
    win = np.mean([f[3] for f in fills])
    mdd = drawdown(fills)
    survive = "YES" if mdd < CAPITAL else "NO"
    print(f"{name:24} {len(fills):>6} {win:>6.1%} {dep:>9.0f} {pnl:>+8.0f} "
          f"{pnl/dep:>+7.2%} {mdd:>7.0f} {mdd/CAPITAL:>7.1%} {survive:>4} "
          f"{pnl/mdd if mdd else 0:>6.1f}")


def main():
    print("loading binance 1s klines...", flush=True)
    base, price, vol, obi = load_binance()
    print("loading parquet book + outcomes...", flush=True)
    ticks = load_ticks()
    print(f"  ticks: {len(ticks['t']):,}\n")
    data = (base, price, vol, ticks, obi)

    print(f"$1000 bankroll, FULL window, 2 months. 'surv?' = maxDD < $1000.")
    print(f"{'caps':24} {'fills':>6} {'win%':>6} {'dep$':>9} {'pnl$':>8} "
          f"{'ROI/$':>7} {'maxDD$':>7} {'DD%cap':>7} {'surv?':>4} {'p/DD':>6}")
    print("-" * 104)
    for label, exp in EXP_LEVELS:
        exact = (30, 60) if exp == 200 else None
        summarize(label, {**SHARED, **caps_for(exp, exact)}, data)

    print("\n--- for contrast: $250 caps but ALSO last-60s gated (on $1000) ---")
    summarize("exp125 + last 60s", {**SHARED, **caps_for(125), "max_t_rem": 60}, data)
    print("-" * 104)
    print("take = 0.16*exp, pos = 0.32*exp ($250-tier ratio), except exp200 which")
    print("uses the literal $1k-tier 30/60. No kill-switch modeled; maxDD<cap is the")
    print("necessary survival condition (sufficient only if losses aren't 1-session).")


if __name__ == "__main__":
    main()
