"""Late-window question for 15m/1h/4h, answered from the bot's OWN live model
marks (logs/calibration.csv) — because the historical replay archive contains
ONLY 5-minute markets (all assets, 100% 300s windows), so replay_binance.py
cannot evaluate 15m/1h/4h at all.

calibration.csv logs, every ~5s, for every active market:
    ts, kind, slug, t_remaining, p_up (blended), model_p, outcome
and writes a final row with the realized outcome (0/1) at settlement.

We join each mark to its market's outcome by slug, and from the blend identity
    p_up = w*model_p + (1-w)*mid ,  w = 0.65 + 0.30*exp(-t_rem/60)
we invert to recover the market's implied mid (≈ the ask the sniper would face).
That lets us, per kind × t_remaining band:
  - calibration: mean model_p vs realized up-rate, Brier score
  - discrimination: AUC of model_p predicting the outcome (0.5 = coin flip)
  - SNIPE-EV PROXY: replay the favourite-only edge gate (ask in [0.50,0.80],
    edge = model_p_side - mid_side > 0.10) using mid as the ask, and measure
    realized ROI per $ and win-rate. (Optimistic: mid < real ask and no fill
    frictions — so a band that is already weak here is damning.)

Run:  .venv/bin/python research/analyze_calibration_by_kind.py
"""
import csv
import math
from collections import defaultdict

import numpy as np

PATH = "logs/calibration.csv"
FEE = 0.07
MIN_ASK, MAX_ASK, MIN_EDGE = 0.50, 0.80, 0.10
KINDS = ["5m", "15m", "1h", "4h"]
# (label, lo, hi]  in seconds remaining
BANDS = [("0-30s", 0, 30), ("30-60s", 30, 60), ("60-120s", 60, 120),
         ("120-300s", 120, 300), ("300-900s", 300, 900),
         ("900-1800s", 900, 1800), ("1800-3600s", 1800, 3600),
         (">3600s", 3600, 1e18)]


def auc(scores, labels):
    """Rank-based AUC (Mann-Whitney). 0.5 = no discrimination."""
    s = np.asarray(scores, float)
    y = np.asarray(labels, int)
    n1, n0 = int(y.sum()), int((1 - y).sum())
    if n1 == 0 or n0 == 0:
        return float("nan")
    order = np.argsort(s, kind="mergesort")
    ranks = np.empty(len(s), float)
    ranks[order] = np.arange(1, len(s) + 1)
    # average ranks for ties
    _, inv, cnt = np.unique(s, return_inverse=True, return_counts=True)
    csum = np.cumsum(cnt)
    avg = {i: (csum[i] - cnt[i] + 1 + csum[i]) / 2 for i in range(len(cnt))}
    ranks = np.array([avg[i] for i in inv])
    return (ranks[y == 1].sum() - n1 * (n1 + 1) / 2) / (n1 * n0)


def load():
    outcome = {}
    rows = []
    with open(PATH) as f:
        for r in csv.DictReader(f):
            if r["outcome"] != "":
                outcome[r["slug"]] = int(r["outcome"])
            elif r["p_up"] and r["model_p"]:
                rows.append((r["kind"], r["slug"], float(r["t_remaining"]),
                             float(r["p_up"]), float(r["model_p"])))
    return outcome, rows


def main():
    outcome, rows = load()
    print(f"loaded {len(rows):,} mark rows, {len(outcome):,} settled markets\n")

    # bucket: per (kind, band) collect model_p, recovered mid, realized outcome
    data = defaultdict(lambda: {"mp": [], "mid": [], "y": []})
    no_mid = defaultdict(int)
    have = defaultdict(int)
    for kind, slug, t_rem, p_up, model_p in rows:
        if slug not in outcome:
            continue
        y = outcome[slug]
        band = next((b[0] for b in BANDS if b[1] < t_rem <= b[2]), None)
        if band is None:
            continue
        key = (kind, band)
        have[key] += 1
        # recover implied market mid by inverting the blend
        w = 0.65 + 0.30 * math.exp(-max(t_rem, 0) / 60.0)
        if abs(p_up - model_p) < 1e-9:   # no book at that tick -> mid unknown
            no_mid[key] += 1
            mid = None
        else:
            mid = (p_up - w * model_p) / (1 - w)
            mid = min(max(mid, 0.0), 1.0)
        data[key]["mp"].append(model_p)
        data[key]["y"].append(y)
        data[key]["mid"].append(mid)

    # ------- VIEW 1: model calibration + discrimination per kind × band -------
    print("=" * 92)
    print("MODEL QUALITY per kind × t_remaining band  (does the edge EXIST early?)")
    print("  up_rate = realized P(up);  AUC<~0.55 => model marks barely beat a coin flip")
    print("=" * 92)
    for kind in KINDS:
        print(f"\n--- {kind} ---")
        print(f"{'band':12} {'n':>7} {'mean_mp':>8} {'up_rate':>8} {'brier':>7} {'AUC':>6}")
        print("-" * 52)
        for label, lo, hi in BANDS:
            d = data[(kind, label)]
            if len(d["y"]) < 30:
                continue
            mp = np.array(d["mp"]); y = np.array(d["y"])
            brier = np.mean((mp - y) ** 2)
            a = auc(mp, y)
            print(f"{label:12} {len(y):>7} {mp.mean():>8.3f} {y.mean():>8.3f} "
                  f"{brier:>7.3f} {a:>6.3f}")

    # ------- VIEW 2: snipe-EV proxy per kind × band -------
    print("\n" + "=" * 92)
    print("SNIPE-EV PROXY per kind × band  (favourite-only, ask=mid, edge>0.10)")
    print("  ROI/$ = realized pnl per $ deployed, fee-netted. OPTIMISTIC (no spread,")
    print("  no fill frictions) — a band weak here is weaker live. %mid = rows w/ a book.")
    print("=" * 92)
    for kind in KINDS:
        print(f"\n--- {kind} ---")
        print(f"{'band':12} {'signals':>8} {'win%':>6} {'ROI/$':>8} {'%mid':>6}")
        print("-" * 46)
        for label, lo, hi in BANDS:
            d = data[(kind, label)]
            if have[(kind, label)] < 30:
                continue
            n_sig = 0
            rois = []
            wins = []
            for mp, mid, y in zip(d["mp"], d["mid"], d["y"]):
                if mid is None:
                    continue
                # favourite-only sniper gate, mid as the ask
                for side_up in (True, False):
                    ask = mid if side_up else 1 - mid
                    if not (MIN_ASK <= ask <= MAX_ASK):
                        continue
                    fair = mp if side_up else 1 - mp
                    edge = fair - ask - FEE * ask * (1 - ask)
                    if edge <= MIN_EDGE:
                        continue
                    won = y if side_up else (1 - y)
                    pnl = won - ask - FEE * ask * (1 - ask)   # per share
                    rois.append(pnl / ask)
                    wins.append(won)
                    n_sig += 1
            pct_mid = 100 * (1 - no_mid[(kind, label)] / max(have[(kind, label)], 1))
            if n_sig == 0:
                print(f"{label:12} {0:>8} {'-':>6} {'-':>8} {pct_mid:>5.0f}%")
                continue
            print(f"{label:12} {n_sig:>8} {np.mean(wins):>6.1%} "
                  f"{np.mean(rois):>+8.2%} {pct_mid:>5.0f}%")

    print("\n" + "-" * 92)
    print("NOTE: marks are sampled every ~5s, so 'signals' counts mark-ticks that would")
    print("qualify, not independent trades (one market contributes many ticks). Read the")
    print("SIGN and STABILITY of ROI/$ across bands, not the absolute trade count.")


if __name__ == "__main__":
    main()
