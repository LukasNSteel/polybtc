"""Find the OPTIMAL late-window per market kind (15m / 1h / 4h; 5m as control),
on the real 15m/1h/4h data downloaded by collect_longmkts.py.

Each kind is swept IN ISOLATION (only that kind's markets, so the $100 exposure
cap reflects that kind alone) over a grid of "trade only in the last N seconds"
gates. We report, per gate:
    fills, win%, $ deployed, PnL, ROI/$ (efficiency), maxDD, PnL/maxDD (risk-adj).
Then exclusive t_remaining bands show WHERE PnL/risk concentrates, and a spread
sensitivity (ask = mid + spread) checks the verdict isn't an artifact of the
optimistic mid-as-ask assumption.

Caveat: absolute $ are optimistic (1-min mid-as-ask, no depth/sub-minute
frictions) — read the RELATIVE shape across gates, and the SIGN/robustness under
the spread sweep, not the headline ROI.

Run:  PYTHONPATH=research .venv/bin/python research/test_longmkts_windows.py
"""
import numpy as np

from replay_longmkts import load_all, run_long, drawdown

SHARED = dict(min_ask=0.50, max_ask=0.80, min_edge=0.10, max_edge=0.20,
              max_take_usd=16, max_position_usd=32, max_exposure_usd=100,
              cooldown=10, race_loss=0.20)

# per-kind candidate gate grids (seconds remaining); capped at the window length
GRID = {
    "5m":  [None, 240, 180, 120, 90, 60, 30],
    "15m": [None, 600, 450, 300, 240, 180, 120, 90, 60, 30],
    "1h":  [None, 1800, 1200, 900, 600, 450, 300, 180, 120, 60],
    "4h":  [None, 7200, 3600, 1800, 1200, 900, 600, 300, 180, 120, 60],
}
BANDS = {
    "5m":  [(180, 300), (120, 180), (60, 120), (30, 60), (0, 30)],
    "15m": [(600, 900), (300, 600), (120, 300), (60, 120), (30, 60), (0, 30)],
    "1h":  [(1800, 3600), (900, 1800), (300, 900), (120, 300), (60, 120), (0, 60)],
    "4h":  [(7200, 14400), (3600, 7200), (1800, 3600), (900, 1800),
            (300, 900), (60, 300), (0, 60)],
}


def kind_data(data, kind):
    base, price, vol, markets, prices = data
    return base, price, vol, [m for m in markets if m[0] == kind], prices


def stats(fills):
    if not fills:
        return None
    dep = sum(f[0] for f in fills); pnl = sum(f[2] for f in fills)
    return dict(n=len(fills), win=np.mean([f[3] for f in fills]), dep=dep,
                pnl=pnl, roi=pnl / dep if dep else 0, mdd=drawdown(fills),
                pdd=pnl / drawdown(fills) if drawdown(fills) else 0)


def prow(label, st, w=16):
    if st is None:
        print(f"{label:{w}} {'0':>6}"); return
    print(f"{label:{w}} {st['n']:>6} {st['win']:>6.1%} {st['dep']:>8.0f} "
          f"{st['pnl']:>+8.0f} {st['roi']:>+7.2%} {st['mdd']:>7.0f} {st['pdd']:>6.1f}")


def phead(w=16):
    print(f"{'':{w}} {'fills':>6} {'win%':>6} {'dep$':>8} {'pnl$':>8} "
          f"{'ROI/$':>7} {'maxDD$':>7} {'p/DD':>6}")
    print("-" * (w + 47))


def main():
    data = load_all()
    print("\n(absolute $ optimistic; compare ACROSS gates within a kind)\n")

    best = {}
    for kind in ["5m", "15m", "1h", "4h"]:
        kd = kind_data(data, kind)
        print("=" * 78)
        print(f"{kind}: cumulative 'last N seconds' gate sweep "
              f"({len(kd[3])} markets)")
        print("=" * 78)
        phead()
        rows = []
        for g in GRID[kind]:
            cfg = {**SHARED} if g is None else {**SHARED, "max_t_rem": g}
            st = stats(run_long(cfg, kd))
            rows.append((g, st))
            prow("full window" if g is None else f"last {g}s", st)
        # pick best by risk-adjusted PnL/DD among gates with a real sample
        scored = [(st["pdd"], g, st) for g, st in rows if st and st["n"] >= 20]
        if scored:
            best[kind] = max(scored, key=lambda x: x[0])

        print(f"\n{kind}: exclusive t_remaining bands (where edge/risk lives)")
        phead()
        for lo, hi in BANDS[kind]:
            cfg = {**SHARED, "min_t_rem": lo, "max_t_rem": hi}
            prow(f"({lo},{hi}]s", stats(run_long(cfg, kd)))
        print()

    # ---------------- spread sensitivity on the candidate window ----------------
    print("=" * 78)
    print("SPREAD SENSITIVITY — ROI/$ under ask = mid + spread (robustness)")
    print("=" * 78)
    probe = {"15m": 300, "1h": 600, "4h": 600}   # candidate windows under test
    print(f"{'kind/policy':22} {'spread':>7} {'fills':>6} {'win%':>6} "
          f"{'ROI/$':>7} {'pnl$':>8} {'p/DD':>6}")
    print("-" * 70)
    for kind, win in probe.items():
        kd = kind_data(data, kind)
        for sp in (0.00, 0.01, 0.02, 0.03):
            for label, gate in (("full", None), (f"last{win}s", win)):
                cfg = {**SHARED, "spread": sp}
                if gate:
                    cfg["max_t_rem"] = gate
                st = stats(run_long(cfg, kd))
                if st is None:
                    print(f"{kind+' '+label:22} {sp:>7.2f} {'0':>6}"); continue
                print(f"{kind+' '+label:22} {sp:>7.2f} {st['n']:>6} "
                      f"{st['win']:>6.1%} {st['roi']:>+7.2%} {st['pnl']:>+8.0f} "
                      f"{st['pdd']:>6.1f}")
        print()

    print("=" * 78)
    print("BEST gate per kind by PnL/maxDD (risk-adjusted):")
    for kind in ["5m", "15m", "1h", "4h"]:
        if kind not in best:
            continue
        pdd, g, st = best[kind]
        win = "full window" if g is None else f"last {g}s"
        print(f"  {kind:4} -> {win:12}  p/DD {pdd:.1f}, ROI/$ {st['roi']:+.2%}, "
              f"win {st['win']:.1%}, pnl ${st['pnl']:+.0f}, {st['n']} fills")


if __name__ == "__main__":
    main()
