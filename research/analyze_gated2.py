"""Two questions on the gated UP snipes:
  1) would they FILL, and would makers ADVERSELY SELECT us? -> measured on the
     REAL shadow_taker attempts (the bot's actual FAK submits), using fill rate +
     post-fill markout (drift_vs_fill on the token we bought; +=favourable, so a
     persistently NEGATIVE markout is the adverse-selection signature).
  2) is there a TIGHTER t_remaining / edge sub-band than [90,180]s that wins more?
     -> measured on the gated window/UP candidates vs the Binance-candle outcome.
"""
import csv
import json
import re
import statistics as st
from collections import defaultdict

fee = lambda a: 0.07 * a * (1 - a)  # noqa: E731

# ---------- outcomes from the 5m candle (resolution proxy) ----------
candle = {int(r["ts"]): float(r["bps"])
          for r in csv.DictReader(open("research/data/btc_5m_240d.csv"))}


def outcome_for(slug):
    m = re.search(r"-(\d+)$", slug)
    bps = candle.get(int(m.group(1))) if m else None
    return None if bps is None else ("up" if bps >= 0 else "dn")


def band(rows, key, lab):
    if not rows:
        print(f"  {lab:18} n=0"); return
    print(f"  {lab:18} n={len(rows):>3}  " + key(rows))


# ================= 1) REAL fills: fillability + adverse selection =================
recs = [json.loads(l) for l in open("logs/shadow_taker.jsonl") if l.strip()]
att = {r["id"]: r for r in recs if r.get("type") == "attempt"}
mk = defaultdict(dict)
for r in recs:
    if r.get("type") == "markout":
        mk[r["id"]][r["horizon_s"]] = r["drift_vs_fill"]

print("=== REAL shadow_taker attempts: fill rate by t_remaining ===")
for lo, hi in [(0, 90), (90, 150), (150, 240)]:
    sub = [r for r in att.values() if lo <= r["t_remaining_s"] < hi]
    if not sub:
        print(f"  t_rem [{lo},{hi})s   n=0"); continue
    f = [r for r in sub if r.get("filled")]
    cap = [r["capture_frac"] for r in f if r.get("capture_frac") is not None]
    print(f"  t_rem [{lo},{hi})s   n={len(sub):>3}  fill {100*len(f)/len(sub):>3.0f}%"
          f"  med capture {100*st.median(cap) if cap else 0:>4.0f}%")

print("\n=== ADVERSE SELECTION: post-fill markout on FILLED snipes "
      "(+=price moved our way) ===")
filled = [r for r in att.values() if r.get("filled") and r["leg"] == "snipe"]
for lo, hi in [(0, 90), (90, 240)]:
    for side in ("up", "dn", None):
        sub = [r for r in filled if lo <= r["t_remaining_s"] < hi
               and (side is None or r["side"] == side)]
        d2 = [mk[r["id"]].get(2.0) for r in sub if 2.0 in mk[r["id"]]]
        d10 = [mk[r["id"]].get(10.0) for r in sub if 10.0 in mk[r["id"]]]
        if not d2:
            continue
        lab = f"t_rem[{lo},{hi}) {side or 'all'}"
        print(f"  {lab:22} n={len(d2):>3}  2s {100*st.mean(d2):>+5.1f}c"
              f"  10s {100*st.mean(d10) if d10 else 0:>+5.1f}c")

# ================= 2) gated window/UP: tighter bound search =================
print("\n=== gated window/UP candidates: outcome by sub-band (find tighter bound) ===")
ups, seen = [], set()
for l in open("logs/shadow_candidates.jsonl"):
    try:
        d = json.loads(l)
    except Exception:
        continue
    if d.get("type") != "candidate" or d.get("reason") != "window" or d["side"] != "up":
        continue
    k = (d["slug"], d["side"])
    if k in seen:
        continue
    seen.add(k)
    o = outcome_for(d["slug"])
    if o is None:
        continue
    won = int(d["side"] == o)
    d["won"], d["pnl"] = won, won - d["seen_ask_px"] - fee(d["seen_ask_px"])
    ups.append(d)


def summ(rows):
    n = len(rows); w = sum(r["won"] for r in rows)
    pnl = sum(r["pnl"] for r in rows) / n
    return f"W{w}/{n-w}  win {100*w/n:>3.0f}%  pnl/sh {100*pnl:>+5.1f}c"


print(f"all window/UP: n={len(ups)}  {summ(ups)}\n")
print("  by t_remaining:")
for lo, hi in [(90, 110), (110, 130), (130, 150), (150, 170), (170, 185)]:
    band([r for r in ups if lo < r["t_remaining_s"] <= hi], summ, f"({lo},{hi}]s")
print("  by net_edge:")
for lo, hi in [(0.10, 0.13), (0.13, 0.18), (0.18, 1)]:
    band([r for r in ups if lo <= r["net_edge"] < hi], summ, f"edge[{lo},{hi})")
print("  by dist_sigma (favourite strength):")
for lo, hi in [(0, 1.0), (1.0, 1.6), (1.6, 9)]:
    band([r for r in ups if lo <= r["dist_sigma"] < hi], summ, f"sig[{lo},{hi})")
print("  by seen_ask_sz (fillability for a ~5-share order):")
for lo, hi in [(0, 8), (8, 30), (30, 1e9)]:
    band([r for r in ups if lo <= r["seen_ask_sz"] < hi], summ, f"sz[{lo},{hi})")
