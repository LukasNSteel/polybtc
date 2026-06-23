"""Per-market-kind late-window study for the $1000 OPTIMISED config.

QUESTION (from a live observation): the bot bought into a 4h market with ~18
minutes still left on the clock. Should 15m / 1h / 4h markets be restricted to a
late "betting window" (only fire in the last N seconds), while the 5m market is
deliberately left FULL window because that is where the full-window edge lives?

This reuses the faithful replay in replay_binance.py (same model, same paper
frictions: race-loss + capture + edge-contention + feed-lag) and the live
config.live1000optimised.yaml gates/caps. The only thing that changes between
runs is the per-kind time gate. Three views:

  A. FULL-WINDOW baseline, broken down per market kind (5m/15m/1h/4h).
  B. WHERE the PnL comes from: for each kind, the exclusive t_remaining bands
     (how much PnL/drawdown the EARLY part of each window actually contributes).
  C. POLICY test: apply a late-window gate to {15m,1h,4h}, keep 5m full, and
     compare total + per-kind PnL, ROI/$ and drawdown vs the full-window baseline.

Run:  PYTHONPATH=research python research/test_late_window_by_kind.py
"""
import numpy as np

from replay_binance import load_binance, load_ticks, run, drawdown

# config.live1000optimised.yaml: the live gates + the OPTIMISED $1000 caps
#   sniper: min_ask .50 / max_ask .80 / min_edge .10 / max_edge .20
#   take16 / pos32 / exp100 (the exp100 seed-sweep winner)
SHARED = dict(min_ask=0.50, max_ask=0.80, min_edge=0.10, max_edge=0.20,
              max_take_usd=16, max_position_usd=32, max_exposure_usd=100,
              cooldown=10, race_loss=0.20, capture=0.30,
              contention=True, feedlag=True, no_scale=True)
CAPITAL = 1000.0
KINDS = ["5m", "15m", "1h", "4h"]


def _stats(fills):
    if not fills:
        return None
    dep = sum(f[0] for f in fills)
    pnl = sum(f[2] for f in fills)
    win = np.mean([f[3] for f in fills])
    mdd = drawdown(fills)
    return dict(n=len(fills), win=win, dep=dep, pnl=pnl,
                roi=pnl / dep if dep else 0.0, mdd=mdd,
                pdd=pnl / mdd if mdd else 0.0)


def _row(label, st, width=30):
    if st is None:
        print(f"{label:{width}} {'0':>6}")
        return
    print(f"{label:{width}} {st['n']:>6} {st['win']:>6.1%} {st['dep']:>9.0f} "
          f"{st['pnl']:>+8.0f} {st['roi']:>+7.2%} {st['mdd']:>7.0f} {st['pdd']:>6.1f}")


def _header(width=30):
    print(f"{'':{width}} {'fills':>6} {'win%':>6} {'dep$':>9} {'pnl$':>8} "
          f"{'ROI/$':>7} {'maxDD$':>7} {'p/DD':>6}")
    print("-" * (width + 47))


def by_kind(fills):
    return {k: [f for f in fills if f[8] == k] for k in KINDS}


def main():
    print("loading binance 1s klines...", flush=True)
    base, price, vol, obi = load_binance()
    print("loading parquet book + outcomes...", flush=True)
    ticks = load_ticks()
    print(f"  ticks: {len(ticks['t']):,}\n")
    data = (base, price, vol, ticks, obi)

    base_fills, _ = run(SHARED, base, price, vol, ticks, obi=obi)
    bk = by_kind(base_fills)

    # ----------------------------------------------------------------- VIEW A
    print("=" * 78)
    print("A. FULL-WINDOW baseline (current config.live1000optimised), per kind")
    print("=" * 78)
    _header()
    _row("ALL kinds", _stats(base_fills))
    print("-" * 77)
    for k in KINDS:
        _row(f"  {k}", _stats(bk[k]))

    # ----------------------------------------------------------------- VIEW B
    print("\n" + "=" * 78)
    print("B. WHERE the PnL comes from — exclusive t_remaining bands, per kind")
    print("   (positive 'early' PnL => trading early HELPS; negative => it hurts)")
    print("=" * 78)
    bands = [("> 300s (early)", 300, 1e9), ("120-300s", 120, 300),
             ("60-120s", 60, 120), ("30-60s", 30, 60), ("0-30s (wire)", 0, 30)]
    for k in KINDS:
        print(f"\n--- {k} ---")
        _header(width=18)
        for label, lo, hi in bands:
            seg = [f for f in bk[k] if lo < f[9] <= hi]
            _row(label, _stats(seg), width=18)

    # ----------------------------------------------------------------- VIEW C
    print("\n" + "=" * 78)
    print("C. POLICY TEST — gate {15m,1h,4h} to a late window, keep 5m FULL")
    print("=" * 78)

    def kind_gate(secs, include_5m=False):
        g = {k: secs for k in ("15m", "1h", "4h")}
        if include_5m:
            g["5m"] = secs
        return g

    policies = {
        "baseline (all full window)": {},
        "15m/1h/4h last 300s; 5m full": {"kind_max_t_rem": kind_gate(300)},
        "15m/1h/4h last 120s; 5m full": {"kind_max_t_rem": kind_gate(120)},
        "15m/1h/4h last 60s;  5m full": {"kind_max_t_rem": kind_gate(60)},
        "15m/1h/4h last 30s;  5m full": {"kind_max_t_rem": kind_gate(30)},
        "ALL kinds last 60s (incl 5m)": {"kind_max_t_rem": kind_gate(60, True)},
    }

    print(f"\n{'policy':32} {'fills':>6} {'win%':>6} {'pnl$':>8} {'ROI/$':>7} "
          f"{'maxDD$':>7} {'p/DD':>6}")
    print("-" * 78)
    results = {}
    for name, override in policies.items():
        fills, _ = run({**SHARED, **override}, base, price, vol, ticks, obi=obi)
        results[name] = fills
        st = _stats(fills)
        if st is None:
            print(f"{name:32} {'0':>6}")
            continue
        print(f"{name:32} {st['n']:>6} {st['win']:>6.1%} {st['pnl']:>+8.0f} "
              f"{st['roi']:>+7.2%} {st['mdd']:>7.0f} {st['pdd']:>6.1f}")

    # per-kind PnL under the most relevant gated policy vs baseline
    print("\n--- per-kind PnL: baseline vs '15m/1h/4h last 60s; 5m full' ---")
    print(f"{'kind':6} {'base pnl$':>10} {'base DD$':>9} | {'gated pnl$':>11} {'gated DD$':>10}")
    print("-" * 56)
    gated = by_kind(results["15m/1h/4h last 60s;  5m full"])
    for k in KINDS:
        b, g = _stats(bk[k]), _stats(gated[k])
        bp = f"{b['pnl']:+.0f}" if b else "0"
        bd = f"{b['mdd']:.0f}" if b else "0"
        gp = f"{g['pnl']:+.0f}" if g else "0"
        gd = f"{g['mdd']:.0f}" if g else "0"
        print(f"{k:6} {bp:>10} {bd:>9} | {gp:>11} {gd:>10}")

    print("\n" + "-" * 78)
    print("Fixed caps, $1000 start, no equity-scaling. ~8 weeks. ROI/$ = pnl per $")
    print("deployed (friction-robust). p/DD = total pnl / max drawdown. Per-kind DD")
    print("is that kind's standalone drawdown (not its marginal book contribution).")


if __name__ == "__main__":
    main()
