"""Does raising sniper.min_ask from 0.50 to 0.55 actually help?

Joins every snipe fill to its settlement across ALL sessions, then compares
the 0.50-0.54 ask bucket against 0.55+ on win-rate, EV/$, total $, and a
bootstrap CI. Also reports the counterfactual: keeping 0.55+ only and what it
means for total dollars vs return-on-capital.

Usage: python3 research/test_min_ask.py
"""
import glob
import math
import random
import re
from collections import defaultdict
from datetime import datetime

FILL_RE = re.compile(
    r"^(?P<ts>[\d-]+ [\d:,]+) \S+\s+INFO\s+FILL (?P<side>UP|DN)\s+(?P<leg>\w+)\s+"
    r"(?P<title>.+?)\s+(?P<sh>[\d.]+) sh @ (?P<px>[\d.]+) "
    r"\(\$(?P<cost>[\d.]+)(?: \+fee (?P<fee>[\d.]+))?\)")
SETTLE_RE = re.compile(
    r"^(?P<ts>[\d-]+ [\d:,]+) \S+\s+INFO\s+SETTLE (?P<title>.+?) -> (?P<out>UP|DOWN) ")


def wilson(k, n, z=1.96):
    if n == 0:
        return (0, 0)
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return ((c - h) / d, (c + h) / d)


def boot(pnls, costs, it=5000):
    if not pnls or sum(costs) == 0:
        return (0, 0)
    n = len(pnls)
    out = []
    for _ in range(it):
        s = [random.randrange(n) for _ in range(n)]
        c = sum(costs[i] for i in s)
        if c:
            out.append(sum(pnls[i] for i in s) / c)
    out.sort()
    return (out[int(.025 * len(out))], out[int(.975 * len(out))])


rows = []
for path in sorted(glob.glob("logs/session_*.log")):
    fills, settles = [], {}
    with open(path) as f:
        for line in f:
            m = FILL_RE.match(line)
            if m and m.group("leg") == "snipe":
                fills.append(m.groupdict())
                continue
            m = SETTLE_RE.match(line)
            if m:
                settles[m.group("title")] = m.group("out")
    for fl in fills:
        out = settles.get(fl["title"])
        if out is None:
            continue
        side = "up" if fl["side"] == "UP" else "dn"
        won = (out == "UP") == (side == "up")
        sh, px, fee = float(fl["sh"]), float(fl["px"]), float(fl["fee"] or 0)
        rows.append(dict(px=px, sh=sh, cost=sh * px, fee=fee, won=won,
                         pnl=(sh if won else 0) - sh * px - fee, sess=path[-14:-4]))

print(f"{len(rows)} settled snipe fills across {len(glob.glob('logs/session_*.log'))} sessions\n")


def summ(label, rr):
    n = len(rr)
    if n == 0:
        print(f"{label:>16}  n=0")
        return
    w = sum(r["won"] for r in rr)
    c = sum(r["cost"] for r in rr)
    p = sum(r["pnl"] for r in rr)
    lo, hi = wilson(w, n)
    elo, ehi = boot([r["pnl"] for r in rr], [r["cost"] for r in rr])
    print(f"{label:>16}  n={n:>4}  win {w/n:>4.0%} [{lo:.0%},{hi:.0%}]  "
          f"cost ${c:>7.0f}  pnl ${p:>+8.2f}  EV/$ {p/c:>+6.1%} [{elo:+.1%},{ehi:+.1%}]")


lo_b = [r for r in rows if r["px"] < 0.55]
hi_b = [r for r in rows if r["px"] >= 0.55]
print("--- ask bucket comparison (ALL sessions) ---")
summ("ask 0.50-0.54", lo_b)
summ("ask 0.55+", hi_b)
summ("ALL (min_ask .50)", rows)
print()

print("--- finer 5c buckets ---")
g = defaultdict(list)
for r in rows:
    g[int(r["px"] * 20) / 20].append(r)
for k in sorted(g):
    summ(f"ask {k:.2f}", g[k])
print()

# counterfactual
tot = sum(r["pnl"] for r in rows)
tot_hi = sum(r["pnl"] for r in hi_b)
cost_lo = sum(r["cost"] for r in lo_b)
ev_hi = tot_hi / sum(r["cost"] for r in hi_b)
print("--- counterfactual of cutting 0.50-0.54 ---")
print(f"current total snipe pnl (min_ask .50): ${tot:+.2f}")
print(f"keep 0.55+ only, idle freed capital:   ${tot_hi:+.2f}  (gives up ${tot-tot_hi:+.2f})")
print(f"redeploy the ${cost_lo:.0f} freed 0.50 capital at 0.55+ EV/$ {ev_hi:+.1%}: "
      f"+${cost_lo*ev_hi:+.2f} -> total ${tot_hi + cost_lo*ev_hi:+.2f}")
