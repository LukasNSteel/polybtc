"""Would-be settlement P&L of the snipes the LIVE gates declined.

Joins logs/shadow_candidates.jsonl (observation-only; no orders placed) to the
settlement outcomes parsed from the session logs (pulled to logs/box_settles.txt).
Breaks down by WHY each was gated (reason: side=DN-paused, window=outside the
[30,90]s live timing window, trend=trend-filter blocked) and by side, with
would-be win% and per-share EV net the 0.07*p(1-p) fee, entering at the seen ask.

CAVEAT: these are SIGNALS, not fills — live the FAK race fills ~50%, so scale the
COUNTS by the fill rate; win% and EV/share carry over. Small n per bucket -> noisy.
"""
import csv
import json
import re
from collections import defaultdict

fee = lambda a: 0.07 * a * (1 - a)  # noqa: E731

# Outcome per market from the Binance 5m candle (the resolution proxy, available
# for EVERY candle — unlike SETTLE lines, which only exist for markets the bot
# actually traded). slug epoch == candle open ts; up iff close>=open (bps>=0).
candle = {}
for row in csv.DictReader(open("research/data/btc_5m_240d.csv")):
    candle[int(row["ts"])] = float(row["bps"])


def outcome_for(slug):
    m = re.search(r"-(\d+)$", slug)
    if not m:
        return None
    bps = candle.get(int(m.group(1)))
    if bps is None:
        return None
    return "up" if bps >= 0 else "dn"

buckets = defaultdict(list)
seen = set()
matched = unmatched = 0
for line in open("logs/shadow_candidates.jsonl", errors="ignore"):
    try:
        d = json.loads(line)
    except Exception:
        continue
    if d.get("type") != "candidate":
        continue
    key = (d["slug"], d["side"], d.get("reason"))
    if key in seen:
        continue
    seen.add(key)
    outc = outcome_for(d["slug"])
    ask = d.get("seen_ask_px")
    if outc is None or not ask:
        unmatched += 1
        continue
    matched += 1
    won = int(d["side"] == outc)
    pnl = won - ask - fee(ask)            # per share, net fee
    rec = {"won": won, "pnl": pnl, "ask": ask, "side": d["side"],
           "reason": d.get("reason"), "trem": d.get("t_remaining_s")}
    buckets[("reason", d.get("reason"))].append(rec)
    buckets[("reason+side", f"{d.get('reason')}/{d['side']}")].append(rec)

print(f"candidates matched to a settlement: {matched}  (unmatched {unmatched})\n")


def show(label, rows):
    if not rows:
        print(f"  {label:24} n=0")
        return
    n = len(rows)
    w = sum(r["won"] for r in rows)
    pnl = sum(r["pnl"] for r in rows) / n
    ev1 = sum(r["pnl"] / r["ask"] for r in rows) / n
    print(f"  {label:24} n={n:>3}  W{w}/{n-w}  win {100*w/n:>3.0f}%  "
          f"pnl/sh {100*pnl:>+6.1f}c  EV/$ {100*ev1:>+6.1f}%")


print("=== gated by REASON (would-be, hold to settle, net fee) ===")
for r in ["side", "window", "trend"]:
    show(r, buckets[("reason", r)])
print("\n=== reason x side ===")
for k in sorted(buckets):
    if k[0] == "reason+side":
        show(k[1], buckets[k])

# window candidates by how far outside the live window they sat
print("\n=== window candidates by t_remaining band (recapture map) ===")
win = buckets[("reason", "window")]
for lo, hi in [(90, 120), (120, 150), (150, 180)]:
    show(f"t_rem ({lo},{hi}]s", [r for r in win if lo < (r["trem"] or 0) <= hi])
print("\nNOTE: signals, not fills (~50% live fill). Win%/EV carry over; scale counts.")
