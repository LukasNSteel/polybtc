"""Backtest on the historical Polymarket BTC up/down dataset.

Inputs (months of real data, ~8 weeks, all 5-minute markets):
  btc_markets.parquet : one row per market, with the resolved `outcome`
  btc_ticks.parquet   : ~1 Hz top-of-book for the UP and DOWN tokens
        bu/au = bid/ask UP, bd/ad = bid/ask DOWN, s* = sizes

WHAT THIS CAN AND CANNOT TEST
-----------------------------
This is the Polymarket *execution venue* (book) plus ground-truth outcomes.
The live bot's edge is BINANCE-SPOT LAG, and there is no Binance price in this
file, so we cannot reproduce the model's `robust fair value` / `edge` here.

What we CAN measure on real data, at scale (14k markets):
  A. The hold-to-resolution EV surface by entry price x time-to-close, after
     the real taker fee -- i.e. is buying the favorite +EV, flat, or -EV?
     This is the economic foundation the whole strategy rests on.
  B. A BOOK-INTERNAL momentum signal (does the price's own recent move predict
     the outcome beyond what the price already implies?) -- a proxy for the
     lag alpha, and an upper-ish bound on what a price-only snipe could earn.

Fee model (bot/markets.py): fee_per_share = 0.07 * p * (1-p).
Per $1 staked at ask a, holding to resolution:
     ret = (win - a)/a - 0.07*(1 - a)
"""
import sys
import numpy as np
import pandas as pd

ARCH = "/Users/lukassteel/Desktop/archive (1)"
FEE_RATE = 0.07


def load():
    m = pd.read_parquet(f"{ARCH}/btc_markets.parquet",
                        columns=["condition_id", "market_start", "market_end",
                                 "outcome", "volume"])
    m = m[m.outcome.isin(["Up", "Down"])].copy()
    m["start_ep"] = (m.market_start.astype("int64") // 10**9)
    m["end_ep"] = (m.market_end.astype("int64") // 10**9)
    m["up_won"] = (m.outcome == "Up").astype(np.int8)
    t = pd.read_parquet(f"{ARCH}/btc_ticks.parquet",
                        columns=["condition_id", "t", "bu", "au", "bd", "ad",
                                 "sau", "sad"])
    t = t.merge(m[["condition_id", "end_ep", "up_won", "volume"]],
                on="condition_id", how="inner")
    t["t_rem"] = t["end_ep"] - t["t"]
    t = t[(t.t_rem >= 0) & (t.t_rem <= 300)]
    return m, t


def fee(a):
    return FEE_RATE * a * (1.0 - a)


def ret_per_dollar(ask, won):
    """Return per $1 staked, buying a side at `ask`, win=1/0, held to resolve."""
    return (won - ask) / ask - FEE_RATE * (1.0 - ask)


def snapshot_at(t, tau, tol=1):
    """One row per market nearest to `tau` seconds before close."""
    cand = t[(t.t_rem >= tau) & (t.t_rem < tau + 1 + tol)].copy()
    cand = cand.sort_values("t_rem").groupby("condition_id", as_index=False).first()
    return cand


def ev_table_for_side(snap, ask_col, won_expr, label):
    """EV of buying a given side at its ask, bucketed by ask price."""
    df = snap[[ask_col]].copy()
    df["ask"] = snap[ask_col]
    df["won"] = won_expr
    df = df[(df.ask > 0.01) & (df.ask < 0.99)]
    df["ret"] = ret_per_dollar(df.ask.values, df.won.values)
    df["bk"] = (df.ask * 10).astype(int) / 10
    g = df.groupby("bk").agg(n=("ret", "size"), winrate=("won", "mean"),
                             ev=("ret", "mean"))
    return g


def main():
    print("loading parquet (this reads ~4.7M ticks)...", flush=True)
    m, t = load()
    print(f"markets with outcome: {len(m):,} | ticks in-window: {len(t):,} "
          f"| up-rate {m.up_won.mean():.3f}\n")

    # ---- A. hold-to-resolution EV surface --------------------------------
    print("=" * 78)
    print("A. HOLD-TO-RESOLUTION EV by entry price x time-to-close (after fee)")
    print("   buying the FAVORITE side (ask in [0.50,0.80], the bot's gate)")
    print("=" * 78)
    taus = [240, 180, 120, 60, 30, 10]
    print(f"{'price':>8}", *[f"{tau:>5}s" for tau in taus], sep="  ")
    # collect favorite-only EV per (bucket, tau)
    surf = {}
    n_by_tau = {}
    for tau in taus:
        snap = snapshot_at(t, tau)
        rows = []
        for side, ask_col, won_col in (("up", "au", snap.up_won),
                                       ("dn", "ad", 1 - snap.up_won)):
            d = pd.DataFrame({"ask": snap[ask_col].values, "won": won_col.values})
            d = d[(d.ask >= 0.50) & (d.ask <= 0.80)]   # FAVORITE gate
            d["ret"] = ret_per_dollar(d.ask.values, d.won.values)
            rows.append(d)
        fav = pd.concat(rows, ignore_index=True)
        fav["bk"] = (fav.ask * 10).astype(int) / 10
        surf[tau] = fav.groupby("bk").ret.mean()
        n_by_tau[tau] = (fav.bk.value_counts(), fav.ret.mean(), len(fav),
                         fav.ret.mean() and (fav.ret > 0).mean())
    for bk in [0.5, 0.6, 0.7]:
        cells = []
        for tau in taus:
            v = surf[tau].get(bk, np.nan)
            cells.append(f"{v:+5.1%}" if pd.notna(v) else "   -- ")
        print(f"{bk:>6.1f}-{bk+0.1:.1f}", *[f"{c:>6}" for c in cells], sep="  ")
    print("\n  net favorite EV/$ (all buckets 0.50-0.80 pooled), by time-to-close:")
    for tau in taus:
        _, mean_ret, n, _ = n_by_tau[tau]
        print(f"    tau={tau:>3}s : {mean_ret:+6.2%}/$   (n={n:,})")

    # ---- the same WITHOUT fee, to isolate fee drag -----------------------
    snap60 = snapshot_at(t, 60)
    rows = []
    for ask_col, won_col in (("au", snap60.up_won), ("ad", 1 - snap60.up_won)):
        d = pd.DataFrame({"ask": snap60[ask_col].values, "won": won_col.values})
        d = d[(d.ask >= 0.50) & (d.ask <= 0.80)]
        rows.append(d)
    fav = pd.concat(rows, ignore_index=True)
    gross = ((fav.won - fav.ask) / fav.ask).mean()
    net = ret_per_dollar(fav.ask.values, fav.won.values).mean()
    print(f"\n  fee drag check @60s: gross {gross:+.2%}/$  ->  net {net:+.2%}/$  "
          f"(fee costs {gross-net:.2%}/$)")

    # ---- B. book-internal momentum signal --------------------------------
    print("\n" + "=" * 78)
    print("B. BOOK-MOMENTUM proxy: does the UP price's own recent move predict")
    print("   the outcome beyond the price itself? (no Binance needed)")
    print("=" * 78)
    t.sort_values(["condition_id", "t"], inplace=True)
    t["up_mid"] = (t.bu + t.au) / 2
    for K in (5, 10):
        t[f"mom{K}"] = t.up_mid - t.groupby("condition_id").up_mid.shift(K)
    snap = snapshot_at(t, 60)
    snap = snap[(snap.au > 0.50) & (snap.au <= 0.80) | (snap.ad > 0.50) & (snap.ad <= 0.80)]
    for K in (5, 10):
        d = snap.dropna(subset=[f"mom{K}"]).copy()
        # strategy: buy the side momentum favors, if it's a favorite in-band
        buy_up = d[f"mom{K}"] > 0
        ask = np.where(buy_up, d.au, d.ad)
        won = np.where(buy_up, d.up_won, 1 - d.up_won)
        ok = (ask >= 0.50) & (ask <= 0.80)
        ask, won = ask[ok], won[ok]
        r = ret_per_dollar(ask, won)
        # control: random side (the price-only baseline at same band)
        print(f"\n  momentum window {K}s  (buy the side the up-price is moving toward):")
        print(f"    follow-momentum favorite: EV {r.mean():+.2%}/$  win {won.mean():.1%}  n={len(r):,}")
        # fade momentum (buy the lagging side)
        buy_up2 = d[f"mom{K}"] < 0
        ask2 = np.where(buy_up2, d.au, d.ad)
        won2 = np.where(buy_up2, d.up_won, 1 - d.up_won)
        ok2 = (ask2 >= 0.50) & (ask2 <= 0.80)
        r2 = ret_per_dollar(ask2[ok2], won2[ok2])
        print(f"    fade-momentum    favorite: EV {r2.mean():+.2%}/$  win {won2[ok2].mean():.1%}  n={len(r2):,}")

    # ---- C. candidate strategies over the full 8 weeks -------------------
    print("\n" + "=" * 78)
    print("C. CANDIDATE ENTRY STRATEGIES (whole dataset, $25 flat stake/trade)")
    print("   t-stat = mean/se; |t|>2 ~ significant. P&L = EV/$ * $25 * n")
    print("=" * 78)
    print(f"{'strategy':46} {'n':>6} {'win%':>6} {'EV/$':>7} {'t':>6} {'P&L$':>8}")

    def eval_strat(tau, lo, hi, mom=None, momK=10):
        snap = snapshot_at(t, tau)
        if mom is not None:
            snap = snap.dropna(subset=[f"mom{momK}"])
        au, ad = snap.au.values, snap.ad.values
        uw = snap.up_won.values
        if mom is None:
            buy_up = au >= ad             # favorite = higher-priced (higher-prob) side
        else:
            mv = snap[f"mom{momK}"].values
            buy_up = (mv > 0) if mom == "follow" else (mv < 0)
        ask = np.where(buy_up, au, ad)
        won = np.where(buy_up, uw, 1 - uw)
        ok = (ask >= lo) & (ask <= hi)
        ask, won = ask[ok], won[ok]
        if len(ask) < 30:
            return None
        r = ret_per_dollar(ask, won)
        se = r.std(ddof=1) / np.sqrt(len(r))
        return len(r), won.mean(), r.mean(), r.mean() / se, r.mean() * 25 * len(r)

    cands = [
        ("favorite 0.50-0.80 @180s (early)", dict(tau=180, lo=.50, hi=.80)),
        ("favorite 0.50-0.80 @60s", dict(tau=60, lo=.50, hi=.80)),
        ("favorite 0.50-0.80 @30s", dict(tau=30, lo=.50, hi=.80)),
        ("strong fav 0.65-0.80 @60s", dict(tau=60, lo=.65, hi=.80)),
        ("strong fav 0.65-0.80 @30s", dict(tau=30, lo=.65, hi=.80)),
        ("mom-follow fav 0.50-0.80 @60s", dict(tau=60, lo=.50, hi=.80, mom="follow")),
        ("mom-follow strong 0.65-0.80 @60s", dict(tau=60, lo=.65, hi=.80, mom="follow")),
        ("mom-follow strong 0.65-0.80 @30s", dict(tau=30, lo=.65, hi=.80, mom="follow")),
    ]
    for name, kw in cands:
        res = eval_strat(**kw)
        if res is None:
            print(f"{name:46} {'(too few)':>6}")
            continue
        n, w, ev, tstat, pnl = res
        print(f"{name:46} {n:>6,} {w:>6.1%} {ev:>+7.2%} {tstat:>+6.1f} {pnl:>+8.0f}")

    print("\n" + "=" * 78)
    print("NOTE: the live bot's actual entries are timed off Binance-spot lag,")
    print("which is NOT in this file. A faithful full replay needs Binance 1s")
    print("BTC price for 2026-03-24..05-18 joined to this book. This run measures")
    print("the EV structure (foundation) and a price-only signal (proxy).")


if __name__ == "__main__":
    main()
