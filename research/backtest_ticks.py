"""Sweep the sniper FAK limit slack (limit_slack_ticks) on the proposed rule set
over the full ~13-week BTC archive. 1 tick = $0.01.

Wider slack = our marketable limit sits further above the signal ask, so an order
survives when the quote ticks up in the speed-bump window (feed_lag) -> MORE
fills. But a recaptured fill pays up to the fresher ask (slack_payup), so the
marginal fills cost more and skew to spots where the book moved -> the trade-off
is fills vs fill-quality. We report fills / win% / PnL / ROI/$ at +2..+5 ticks.

Rule set held fixed at the live PROPOSED stack:
  max_ask 0.80, min_ask 0.50, dist_sigma_min 0.50, edge (0.10,0.30],
  5m window [30,90]s, trend_filter_sigma 1.0.

CAVEAT: the replay's feed_lag models the richen-in-flight miss; it does NOT model
the live 'book collapsed below the favourite' adverse fill (logged live as
ADVERSE SNIPE FILL). So treat wider-slack fills here as an UPPER bound on quality
— the live min_ask floor + close_buffer are what cap the downside.
"""
import numpy as np
import replay_binance as R


def stats(fills):
    if not fills:
        return (0, 0.0, 0.0, 0.0, 0.0)
    dep = sum(f[0] for f in fills)
    pnl = sum(f[2] for f in fills)
    win = float(np.mean([f[3] for f in fills]))
    return (len(fills), win, dep, pnl, pnl / dep if dep else 0.0)


def main():
    print("loading binance 1s klines (~13wk)...", flush=True)
    base, price, vol, obi = R.load_binance()
    print("loading parquet book + outcomes...", flush=True)
    ticks = R.load_ticks()

    proposed = dict(min_ask=0.50, max_ask=0.80, min_edge=0.10, max_edge=0.30,
                    max_take_usd=10, max_position_usd=10, max_exposure_usd=60,
                    cooldown=10, no_scale=True,
                    kind_only="5m", dist_sigma_min=0.50,
                    min_t_rem=30, max_t_rem=90, trend_filter_sigma=1.0,
                    contention=True, feedlag=True, slack_payup=True)

    regimes = {
        "REALISTIC (race .20 / cap .30)": dict(race_loss=0.20, capture=0.30),
        "HARSH (race .72 / cap .10)":     dict(race_loss=0.72, capture=0.10),
    }
    tick = 0.01
    slacks = [(2, 0.02), (3, 0.03), (5, 0.05), (8, 0.08), (12, 0.12), (20, 0.20)]

    for rname, rcfg in regimes.items():
        print(f"\n=== {rname} — PROPOSED stack, slack sweep ===")
        hdr = f"{'limit_slack':>11} {'fills':>6} {'win%':>6} {'dep$':>8} {'pnl$':>8} {'ROI/$':>7} {'d fills':>8} {'d pnl$':>8}"
        print(hdr); print("-" * len(hdr))
        base_n = base_pnl = None
        for nt, sl in slacks:
            cfg = {**proposed, **rcfg, "limit_slack": sl}
            fills, _ = R.run(cfg, base, price, vol, ticks, obi=obi)
            n, win, dep, pnl, roi = stats(fills)
            if base_n is None:
                base_n, base_pnl = n, pnl
            print(f"{('+%d tick' % nt):>11} {n:>6} {win:>6.1%} {dep:>8.0f} "
                  f"{pnl:>+8.0f} {roi:>+7.1%} {n-base_n:>+8} {pnl-base_pnl:>+8.0f}")
    print("\nd fills / d pnl$ = change vs the current +2-tick setting.")


if __name__ == "__main__":
    main()
