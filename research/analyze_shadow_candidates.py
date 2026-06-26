"""Counterfactual EV of would-be snipes the live gates declined.

Reads logs/shadow_candidates.jsonl (written live, observation-only — no orders
were placed) and joins each candidate to its settlement outcome via the SETTLE
lines in logs/session_*.log (same join as backtest_window_trend.py). Answers, on
REAL forward data:
  * WINDOW: would widening max_t_rem_sec_5m recapture winners or losers? (broken
    out by t_remaining band so we can see WHERE in the wider window EV lives.)
  * TREND : of the fires the trend filter blocked, would they have won?

Caveats: a candidate is a SIGNAL that qualified, not a fill — going forward the
live FAK race fills only ~40-45% of attempts, so multiply win/loss COUNTS by the
fill rate for a forward estimate; win RATE and per-$ EV carry over. P&L is an
estimate from the seen ask (we never actually traded these).

Run:  .venv/bin/python -m research.analyze_shadow_candidates
"""
import json
import re
from glob import glob

FEE_RATE = 0.07
SETTLE = re.compile(r"SETTLE (.+?) -> (UP|DOWN) ")


def load_settles():
    out = {}
    for path in glob("logs/session_*.log"):
        for line in open(path, errors="ignore"):
            m = SETTLE.search(line)
            if m:
                title, outc = m.groups()
                out[title.strip()] = "up" if outc == "UP" else "dn"
    return out


def per_dollar(ask, won):
    if not ask:
        return 0.0
    return (won / ask - 1.0) - FEE_RATE * (1.0 - ask)


def evaluate(rows, label):
    if not rows:
        print(f"  {label:30} n=0")
        return
    n = len(rows)
    w = sum(r["won"] for r in rows)
    ev = sum(r["ev"] for r in rows) / n
    print(f"  {label:30} n={n:>3}  W{w}/{n - w}  win {w / n * 100:>3.0f}%  "
          f"est EV {ev:>+6.1%}/$")


def main():
    settles = load_settles()
    rows = []
    seen = set()
    for line in open("logs/shadow_candidates.jsonl", errors="ignore"):
        try:
            d = json.loads(line)
        except Exception:
            continue
        if d.get("type") != "candidate":
            continue
        outc = settles.get((d.get("title") or "").strip())
        ask = d.get("seen_ask_px")
        if outc is None or ask is None:
            continue
        kdedup = (d["slug"], d["side"], d.get("reason"))
        if kdedup in seen:
            continue  # one decision per (market, side, reason): earliest wins
        seen.add(kdedup)
        won = int(d["side"] == outc)
        rows.append({**d, "won": won, "ev": per_dollar(ask, won)})

    print(f"settled shadow candidates: {len(rows)} "
          f"(of which window={sum(r['reason'] == 'window' for r in rows)}, "
          f"trend={sum(r['reason'] == 'trend' for r in rows)})\n")
    if not rows:
        print("no settled candidates yet — let it run longer.")
        return

    print("=== WINDOW counterfactuals (would a wider max_t_rem_sec recapture?) ===")
    win = [r for r in rows if r["reason"] == "window"]
    evaluate(win, "all window candidates")
    for lo, hi in [(90, 120), (120, 150), (150, 180)]:
        band = [r for r in win if lo < r["t_remaining_s"] <= hi]
        evaluate(band, f"  t_rem ({lo},{hi}]s")

    print("\n=== TREND counterfactuals (fires the trend filter blocked) ===")
    tr = [r for r in rows if r["reason"] == "trend"]
    evaluate(tr, "all trend-blocked candidates")
    for side in ("up", "dn"):
        evaluate([r for r in tr if r["side"] == side], f"  side {side}")

    print("\n=== by |trend_z| band (all candidates) — calibrate trend_filter ===")
    for lo, hi in [(0, 0.5), (0.5, 1.0), (1.0, 1.5), (1.5, 99)]:
        band = [r for r in rows if lo <= abs(r.get("trend_z") or 0) < hi]
        evaluate(band, f"  |z| [{lo},{hi})")

    print("\nNOTE: these are SIGNALS, not fills (~40-45% would actually fill "
          "live). Win% and EV/$ carry over; scale counts by the fill rate.")


if __name__ == "__main__":
    main()
