"""Per-leg P&L attribution from a session log.

Parses FILL and SETTLE lines and attributes settlement payouts to the leg
(mm / snipe / scalp) that bought the shares.
"""

import re
import sys
from collections import defaultdict

FILL = re.compile(
    r"^(?P<ts>\S+ \S+) exec\s+INFO\s+FILL (?P<side>UP|DN)\s+(?P<leg>\w+)\s+"
    r"(?P<title>.+?) (?P<shares>[\d.]+) sh @ (?P<price>[\d.]+) "
    r"\(\$(?P<usd>[\d.]+)(?: \+fee (?P<fee>[\d.]+))?\)")
SETTLE = re.compile(
    r"^(?P<ts>\S+ \S+) exec\s+INFO\s+SETTLE (?P<title>.+?) -> (?P<outcome>UP|DOWN) "
    r"\| payout \$(?P<payout>[\d.-]+) cost \$(?P<cost>[\d.-]+) pnl \$(?P<pnl>[+\d.-]+)")

fills = defaultdict(lambda: defaultdict(lambda: {"sh": 0.0, "cost": 0.0, "n": 0}))
fill_rows = []

path = sys.argv[1] if len(sys.argv) > 1 else "logs/session_1781170769.log"
settles = {}
order = []
for line in open(path):
    m = FILL.match(line)
    if m:
        d = m.groupdict()
        key = (d["leg"], d["side"])
        f = fills[d["title"]][key]
        f["sh"] += float(d["shares"])
        f["cost"] += float(d["usd"]) + float(d["fee"] or 0)
        f["n"] += 1
        fill_rows.append((d["ts"], d["title"], d["leg"], d["side"],
                          float(d["shares"]), float(d["price"])))
        continue
    m = SETTLE.match(line)
    if m:
        d = m.groupdict()
        settles[d["title"]] = d["outcome"]
        order.append(d["title"])

leg_tot = defaultdict(lambda: {"pnl": 0.0, "cost": 0.0, "n": 0, "wins": 0})
print(f"{'market':<55} {'leg':<6} {'side':<4} {'n':>3} {'shares':>7} "
      f"{'cost':>8} {'payout':>8} {'pnl':>8}")
for title in order:
    if title not in fills:
        continue  # restored position, fills not in this log
    out = settles[title]
    win_side = "UP" if out == "UP" else "DN"
    for (leg, side), f in sorted(fills[title].items()):
        payout = f["sh"] if side == win_side else 0.0
        pnl = payout - f["cost"]
        t = leg_tot[leg]
        t["pnl"] += pnl
        t["cost"] += f["cost"]
        t["n"] += f["n"]
        t["wins"] += 1 if pnl > 0 else 0
        print(f"{title:<55} {leg:<6} {side:<4} {f['n']:>3} {f['sh']:>7.0f} "
              f"{f['cost']:>8.2f} {payout:>8.2f} {pnl:>+8.2f}")

print("\n=== per-leg totals (settled markets only) ===")
for leg, t in sorted(leg_tot.items()):
    print(f"{leg:<6} fills {t['n']:>4}  cost ${t['cost']:>8.2f}  pnl ${t['pnl']:>+9.2f}")

# how often did MM buy the losing side, and at what price?
mm_lose_cost = mm_win_cost = 0.0
mm_lose_n = mm_win_n = 0
px_buckets = defaultdict(lambda: [0, 0.0, 0.0])  # bucket -> [n, cost, payout]
for title, legs in fills.items():
    if title not in settles:
        continue
    win_side = "UP" if settles[title] == "UP" else "DN"
    for (leg, side), f in legs.items():
        if leg != "mm":
            continue
        if side == win_side:
            mm_win_cost += f["cost"]; mm_win_n += f["n"]
        else:
            mm_lose_cost += f["cost"]; mm_lose_n += f["n"]
for ts, title, leg, side, sh, px in fill_rows:
    if leg != "mm" or title not in settles:
        continue
    win = (side == "UP") == (settles[title] == "UP")
    b = px_buckets[round(px // 0.1 * 0.1, 1)]
    b[0] += 1
    b[1] += sh * px
    b[2] += sh if win else 0.0

print(f"\nMM fills on WINNING side: {mm_win_n:>4} (${mm_win_cost:.2f})")
print(f"MM fills on LOSING  side: {mm_lose_n:>4} (${mm_lose_cost:.2f})")
print("\nMM pnl by fill price bucket:")
for b in sorted(px_buckets):
    n, cost, pay = px_buckets[b]
    print(f"  {b:.1f}-{b + 0.1:.1f}: {n:>3} fills  cost ${cost:>7.2f}  "
          f"payout ${pay:>7.2f}  pnl ${pay - cost:>+8.2f}")
