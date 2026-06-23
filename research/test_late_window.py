"""Experiment: does restricting the sniper to the last N seconds of a market
help or hurt -- evaluated at the ACTUAL deployment capital tiers ($1000 & $250)?

Reuses the faithful replay engine in replay_binance.py (same model, same paper
frictions: race-loss + capture + edge-contention + feed-lag). Everything is held
identical across runs except the time-to-resolution gate and the sizing caps.

Caps are read straight from the live configs:
  $1000 tier (config.yaml):        take30 / pos60  / exp200
  $250  tier (config.live250.yaml): take20 / pos40  / exp125
Equity-scaling is disabled (no_scale) so the comparison is clean and reflects
the *starting* caps you deploy with.

Run:  PYTHONPATH=research python research/test_late_window.py
"""
import numpy as np

from replay_binance import load_binance, load_ticks, run, drawdown


# shared edge/ask gates (identical across both live configs)
SHARED = dict(min_ask=0.50, max_ask=0.80, min_edge=0.10, max_edge=0.20,
              cooldown=10, race_loss=0.20, capture=0.30,
              contention=True, feedlag=True, no_scale=True)

TIERS = {
    "$1000": dict(capital=1000, max_take_usd=30, max_position_usd=60,
                  max_exposure_usd=200),
    "$250":  dict(capital=250,  max_take_usd=20, max_position_usd=40,
                  max_exposure_usd=125),
}


def summarize(name, cfg, capital, data):
    base, price, vol, ticks, obi = data
    fills, _ = run(cfg, base, price, vol, ticks, obi=obi)
    if not fills:
        print(f"{name:36} {'0':>6}")
        return
    dep = sum(f[0] for f in fills)
    pnl = sum(f[2] for f in fills)
    win = np.mean([f[3] for f in fills])
    mdd = drawdown(fills)
    print(f"{name:36} {len(fills):>6} {win:>6.1%} {dep:>9.0f} {pnl:>+8.0f} "
          f"{pnl/dep:>+7.2%} {mdd:>7.0f} {mdd/capital:>7.1%} {pnl/mdd if mdd else 0:>7.1f}")


def header():
    print(f"{'scenario':36} {'fills':>6} {'win%':>6} {'dep$':>9} {'pnl$':>8} "
          f"{'ROI/$':>7} {'maxDD$':>7} {'DD%cap':>7} {'pnl/DD':>7}")
    print("-" * 100)


def run_tier(label, tier, data):
    cap = tier.pop("capital")
    base_cfg = {**SHARED, **tier}
    print(f"\n############  {label} TIER  (caps: take{tier['max_take_usd']}/"
          f"pos{tier['max_position_usd']}/exp{tier['max_exposure_usd']})  ############")

    print("\n--- CUMULATIVE: trade only in the LAST N seconds ---")
    header()
    summarize("full window (baseline)", {**base_cfg}, cap, data)
    summarize("last 90s only", {**base_cfg, "max_t_rem": 90}, cap, data)
    summarize("last 60s only", {**base_cfg, "max_t_rem": 60}, cap, data)
    summarize("last 30s only", {**base_cfg, "max_t_rem": 30}, cap, data)

    print("\n--- EXCLUSIVE bands: where the PnL comes from ---")
    header()
    summarize("early: t_rem > 90", {**base_cfg, "min_t_rem": 90}, cap, data)
    summarize("band (60, 90]", {**base_cfg, "min_t_rem": 60, "max_t_rem": 90}, cap, data)
    summarize("band (30, 60]", {**base_cfg, "min_t_rem": 30, "max_t_rem": 60}, cap, data)
    summarize("band (0, 30]", {**base_cfg, "max_t_rem": 30}, cap, data)


def main():
    print("loading binance 1s klines...", flush=True)
    base, price, vol, obi = load_binance()
    print("loading parquet book + outcomes...", flush=True)
    ticks = load_ticks()
    print(f"  ticks: {len(ticks['t']):,}")
    data = (base, price, vol, ticks, obi)

    for label, tier in TIERS.items():
        run_tier(label, dict(tier), data)

    print("-" * 100)
    print("Fixed starting caps, no equity-scaling. ROI/$ = pnl per $ deployed.")
    print("DD%cap = max drawdown as a fraction of starting capital. ~8 weeks of markets.")


if __name__ == "__main__":
    main()
