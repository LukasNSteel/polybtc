"""Does distance-to-strike improve the snipe?

The live model already turns spot-vs-open into a probability via
    d = log(spot/open) / (vol * sqrt(t_remaining))
which is exactly "how many standard deviations is spot above the strike, given
the time left." This script asks whether GATING on that physical distance — on
top of the existing edge gate — improves outcomes, i.e. should we only fire when
spot has already moved in our favour by >= k sigma (or >= $X)?

Two views, both on the real 8-week dataset (Binance 1s spot joined to the
Polymarket book + ground-truth outcomes via research/replay_binance.py):

  1. DIAGNOSTIC (friction-free, held to resolution): of the trades the bot
     WANTS to make (edge gate passes), bucket by favourable distance and show
     win% and EV/$. If far-from-strike entries win more, the gate has alpha.

  2. GATED REPLAY (full fill realism): re-run the sniper with a distance gate
     and compare fills / win% / ROI-per-$ / drawdown to the ungated baseline.

Run:  .venv/bin/python -m research.test_distance_gate
"""
import numpy as np
import pandas as pd

from research.replay_binance import (
    load_binance, load_ticks, compute_features, run, drawdown,
)

FEE = 0.07


def ret_per_dollar(ask, won):
    return (won - ask) / ask - FEE * (1.0 - ask)


# Study baseline = the CURRENT live gates (favourite-only, edge in (0.10, 0.15]).
# Caps mirror config.live165 (no equity scaling, so $/ROI are comparable across
# scenarios). Fill frictions are the prior studies' values; ROI/$ is the
# friction-robust metric and the diagnostic below is friction-free.
BASE_CFG = dict(
    min_ask=0.50, max_ask=0.80, min_edge=0.10, max_edge=0.15,
    max_take_usd=10, max_position_usd=10, max_exposure_usd=60,
    cooldown=10, race_loss=0.20, capture=0.30,
    contention=True, feedlag=True, no_scale=True, seed=7,
)


def candidate_entries(F):
    """One row per (market, side) at the FIRST tick its edge gate passes — the
    moment the bot would first fire — with the favourable distance signed toward
    the bet side and the held-to-resolution outcome."""
    cond = F["d"]["condition_id"]
    uw = F["d"]["up_won"]
    au, ad = F["au"], F["ad"]
    ds, du = F["dist_sigma"], F["dist_usd"]
    eu, ed = F["edge_up"], F["edge_dn"]
    tr, kind = F["t_rem"], F["kind"]
    frames = []
    for side, mask, ask, won, edge, favs, favu in (
        ("up", F["cand_up"], au, uw, eu, ds, du),
        ("dn", F["cand_dn"], ad, 1 - uw, ed, -ds, -du),
    ):
        i = np.where(mask)[0]
        frames.append(pd.DataFrame({
            "cond": cond[i], "side": side,
            "fav_sigma": favs[i], "fav_usd": favu[i],
            "edge": edge[i], "ask": ask[i], "won": won[i].astype(float),
            "t_rem": tr[i], "kind": kind[i],
        }))
    e = pd.concat(frames, ignore_index=True)
    # first qualifying tick per (market, side); arrays are time-sorted ascending
    e = e.groupby(["cond", "side"], as_index=False).first()
    e["ret"] = ret_per_dollar(e.ask.values, e.won.values)
    e["gross"] = (e.won - e.ask) / e.ask
    return e


def bucket_table(e, col, bins, labels):
    e = e.copy()
    e["bk"] = pd.cut(e[col], bins=bins, labels=labels)
    g = e.groupby("bk", observed=True).agg(
        n=("ret", "size"), win=("won", "mean"),
        gross=("gross", "mean"), net=("ret", "mean"),
    )
    return g


def main():
    print("loading binance 1s klines + parquet book...", flush=True)
    base, price, vol, obi = load_binance()
    ticks = load_ticks()
    F = compute_features(BASE_CFG, base, price, vol, ticks, obi)
    e = candidate_entries(F)

    print(f"\ncandidate trades (edge gate, favourite 0.50-0.80, edge in "
          f"(0.10,0.15]): {len(e):,}  win {e.won.mean():.1%}  "
          f"net EV {e.ret.mean():+.2%}/$\n")

    # --- how often is the bot betting the side spot is NOT on? ---
    wrong = (e.fav_sigma < 0)
    print(f"entries where spot is on the WRONG side of strike (fav_sigma<0): "
          f"{wrong.mean():.1%}  ->  win {e.won[wrong].mean():.1%}  "
          f"net {e.ret[wrong].mean():+.2%}/$")
    right = ~wrong
    print(f"entries where spot is on the favoured side   (fav_sigma>=0): "
          f"{right.mean():.1%}  ->  win {e.won[right].mean():.1%}  "
          f"net {e.ret[right].mean():+.2%}/$\n")

    print("=" * 78)
    print("1. EV BY SIGMA DISTANCE  (favourable d at entry; all kinds pooled)")
    print("   d = log(spot/open)/(vol*sqrt(t_rem)); friction-free, held to close")
    print("=" * 78)
    sig = bucket_table(
        e, "fav_sigma",
        bins=[-np.inf, 0, 0.25, 0.5, 1.0, 1.5, 2.0, np.inf],
        labels=["<0", "0-0.25", "0.25-0.5", "0.5-1.0", "1.0-1.5", "1.5-2.0", ">2.0"],
    )
    print(f"{'sigma band':>12} {'n':>7} {'win%':>7} {'gross/$':>9} {'net/$':>9}")
    for bk, r in sig.iterrows():
        print(f"{str(bk):>12} {int(r.n):>7,} {r.win:>7.1%} {r.gross:>+9.2%} {r.net:>+9.2%}")

    print("\n" + "=" * 78)
    print("2. EV BY DOLLAR DISTANCE  (|spot-open| toward the bet) — 5m markets only")
    print("   ($ distance is kind-dependent; 5m is the dominant / your main kind)")
    print("=" * 78)
    e5 = e[e.kind == "5m"]
    usd = bucket_table(
        e5, "fav_usd",
        bins=[-np.inf, 0, 10, 25, 50, 100, np.inf],
        labels=["<0", "0-10", "10-25", "25-50", "50-100", ">100"],
    )
    print(f"{'$ band':>12} {'n':>7} {'win%':>7} {'gross/$':>9} {'net/$':>9}")
    for bk, r in usd.iterrows():
        print(f"{str(bk):>12} {int(r.n):>7,} {r.win:>7.1%} {r.gross:>+9.2%} {r.net:>+9.2%}")

    scenarios = {
        "baseline (no distance gate)": {},
        "sigma > 0 (favoured side only)": dict(dist_sigma_min=1e-9),
        "sigma >= 0.5": dict(dist_sigma_min=0.5),
        "sigma >= 1.0  (your '1 stdev')": dict(dist_sigma_min=1.0),
        "sigma >= 1.5": dict(dist_sigma_min=1.5),
        "$ >= 25": dict(dist_usd_min=25),
        "$ >= 50  (your '$50')": dict(dist_usd_min=50),
        "$ >= 100": dict(dist_usd_min=100),
    }
    # Two friction regimes: the prior studies' optimistic values, and the
    # LIVE-calibrated ones measured from shadow_taker.jsonl (the snipe loses ~72%
    # of races and captures ~10% of displayed size). The live regime is the
    # honest test of "does the distance edge survive our real fills?".
    regimes = {
        "3a. OPTIMISTIC frictions (race 0.20, capture 0.30)":
            dict(race_loss=0.20, capture=0.30),
        "3b. LIVE-CALIBRATED frictions (race 0.72, capture 0.10)":
            dict(race_loss=0.72, capture=0.10),
    }
    for rlabel, rfric in regimes.items():
        print("\n" + "=" * 84)
        print(f"{rlabel}  — live165 caps, ~8 weeks")
        print("   ROI/$ = pnl per $ deployed (friction-robust). Same edge gate;")
        print("   the ONLY change per row is the added distance requirement.")
        print("=" * 84)
        print(f"{'scenario':34} {'fills':>6} {'win%':>6} {'dep$':>8} {'pnl$':>8} "
              f"{'ROI/$':>7} {'maxDD$':>7}")
        print("-" * 84)
        for name, override in scenarios.items():
            cfg = {**BASE_CFG, **rfric, **override}
            fills, realized = run(cfg, base, price, vol, ticks, obi=obi)
            if not fills:
                print(f"{name:34} {'0':>6}")
                continue
            dep = sum(f[0] for f in fills)
            pnl = sum(f[2] for f in fills)
            win = np.mean([f[3] for f in fills])
            mdd = drawdown(fills)
            print(f"{name:34} {len(fills):>6,} {win:>6.1%} {dep:>8.0f} {pnl:>+8.0f} "
                  f"{pnl/dep:>+7.1%} {mdd:>7.0f}")
    print("-" * 84)
    print("NOTE: even 'live' frictions run at 1 Hz, so sub-second adverse selection")
    print("is only approximated. Sections 1-2 are friction-free (held to resolution).")


if __name__ == "__main__":
    main()
