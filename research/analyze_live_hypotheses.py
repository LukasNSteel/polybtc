"""Out-of-sample hypothesis tests on the most recent session's live paper fills.

Parses SNIPE intents, FILLs and SETTLEs from a session log, joins fills to
settlements, then:
  - reports headline PnL with a market-clustered bootstrap CI
  - daily breakdown + equity curve / max drawdown from the strategy heartbeat
  - tests several counterfactual strategy filters (what PnL would each rule
    have produced on the same fills)

Usage: python research/analyze_live_hypotheses.py [logs/session_*.log]
"""

import glob
import random
import re
import sys
from collections import defaultdict
from datetime import datetime

random.seed(0)

paths = sys.argv[1:] or [sorted(glob.glob("logs/session_*.log"))[-1]]

SNIPE_RE = re.compile(
    r"^(?P<ts>[\d-]+ [\d:,]+) \S+\s+INFO\s+SNIPE (?P<title>.+?) (?P<side>UP|DN): "
    r"ask (?P<ask>[\d.]+) \+ fee (?P<fee>[\d.]+) vs robust (?P<robust>[\d.]+) "
    r"\(blend (?P<blend>[\d.]+), edge (?P<edge>[\d.]+), \$(?P<usd>[\d.]+)\)"
)
FILL_RE = re.compile(
    r"^(?P<ts>[\d-]+ [\d:,]+) \S+\s+INFO\s+FILL (?P<side>UP|DN)\s+(?P<leg>\w+)\s+"
    r"(?P<title>.+?)\s+(?P<sh>[\d.]+) sh @ (?P<px>[\d.]+) \(\$(?P<cost>[\d.]+)(?: \+fee (?P<fee>[\d.]+))?\)"
)
SETTLE_RE = re.compile(
    r"^(?P<ts>[\d-]+ [\d:,]+) \S+\s+INFO\s+SETTLE (?P<title>.+?) -> (?P<out>UP|DOWN) "
    r"\| payout \$(?P<pay>[\d.]+) cost \$(?P<cost>-?[\d.]+) pnl \$(?P<pnl>[+-][\d.]+)"
)
HEARTBEAT_RE = re.compile(r"cash \$(?P<cash>[-\d.]+) \| equity \$(?P<eq>[-\d.]+)")


def parse_ts(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S,%f")


def kind_of(title):
    m = re.search(r"(\d+):(\d+)(AM|PM)-(\d+):(\d+)(AM|PM)", title)
    if not m:
        return "1h" if re.search(r"\d+(AM|PM) ET", title) else "?"
    h1, m1, ap1, h2, m2, ap2 = m.groups()
    t1 = (int(h1) % 12 + (12 if ap1 == "PM" else 0)) * 60 + int(m1)
    t2 = (int(h2) % 12 + (12 if ap2 == "PM" else 0)) * 60 + int(m2)
    d = (t2 - t1) % (24 * 60)
    return {5: "5m", 15: "15m", 60: "1h", 240: "4h"}.get(d, f"{d}m")


fills, snipes, settles = [], [], {}
equity_pts = []

for path in paths:
    with open(path) as f:
        for line in f:
            m = SNIPE_RE.match(line)
            if m:
                snipes.append(m.groupdict())
                continue
            m = FILL_RE.match(line)
            if m:
                fills.append(m.groupdict())
                continue
            m = SETTLE_RE.match(line)
            if m:
                settles[m.groupdict()["title"]] = m.groupdict()
                continue
            m = HEARTBEAT_RE.search(line)
            if m:
                ts = parse_ts(line[:23])
                equity_pts.append((ts, float(m.group("eq"))))

rows = []
for f in fills:
    s = settles.get(f["title"])
    if not s:
        continue
    side = "up" if f["side"] == "UP" else "dn"
    won = (s["out"] == "UP") == (side == "up")
    sh, px, fee = float(f["sh"]), float(f["px"]), float(f["fee"] or 0)
    pnl = (sh if won else 0.0) - sh * px - fee
    edge = robust = None
    ft = parse_ts(f["ts"])
    best = None
    for sn in snipes:
        if sn["title"] == f["title"] and sn["side"] == f["side"]:
            st = parse_ts(sn["ts"])
            if st <= ft and (best is None or st > best):
                best, edge, robust = st, float(sn["edge"]), float(sn["robust"])
    rows.append(dict(title=f["title"], kind=kind_of(f["title"]), leg=f["leg"],
                     side=side, px=px, sh=sh, cost=sh * px, fee=fee, won=won,
                     pnl=pnl, edge=edge, robust=robust, ts=ft,
                     day=ft.strftime("%m-%d")))


def boot_ci(rows, n=5000):
    """Market-clustered bootstrap of ret/$1 (resample whole markets)."""
    if not rows:
        return (0, 0, 0)
    by_mkt = defaultdict(list)
    for r in rows:
        by_mkt[r["title"]].append(r)
    mkts = list(by_mkt)
    rets = []
    for _ in range(n):
        samp = [random.choice(mkts) for _ in mkts]
        pnl = cost = 0.0
        for mk in samp:
            for r in by_mkt[mk]:
                pnl += r["pnl"]
                cost += r["cost"]
        if cost:
            rets.append(pnl / cost)
    rets.sort()
    return (rets[int(0.025 * len(rets))], sum(rets) / len(rets),
            rets[int(0.975 * len(rets))])


def summary(name, rs):
    pnl = sum(r["pnl"] for r in rs)
    cost = sum(r["cost"] for r in rs)
    if not rs or cost == 0:
        print(f"{name:<34} n=0")
        return
    win = sum(r["won"] for r in rs) / len(rs)
    lo, mid, hi = boot_ci(rs)
    nmkt = len({r["title"] for r in rs})
    print(f"{name:<34} n={len(rs):>3} mkts={nmkt:>3} win={win:>4.0%} "
          f"cost=${cost:>8.0f} pnl=${pnl:>+8.0f} ret/$1={pnl/cost:>+6.1%} "
          f"CI[{lo:+.1%},{hi:+.1%}]")


snipe = [r for r in rows if r["leg"] == "snipe"]
print(f"=== {paths[-1]} ===")
print(f"{len(fills)} fills, {len(rows)} joined, {len(snipe)} snipe fills\n")

print("--- HEADLINE (all joined fills) ---")
summary("all legs", rows)
summary("snipe only", snipe)
print()

print("--- daily snipe PnL (consistency / are profits broad-based?) ---")
by_day = defaultdict(list)
for r in snipe:
    by_day[r["day"]].append(r)
green = 0
for d in sorted(by_day):
    g = by_day[d]
    p = sum(x["pnl"] for x in g)
    c = sum(x["cost"] for x in g)
    green += p > 0
    print(f"  {d}  n={len(g):>3}  cost=${c:>7.0f}  pnl=${p:>+8.0f}  ret={p/c:>+6.1%}")
print(f"  -> {green}/{len(by_day)} days green\n")

# equity curve / max drawdown
if equity_pts:
    eqs = [e for _, e in equity_pts]
    peak = eqs[0]
    maxdd = 0.0
    for e in eqs:
        peak = max(peak, e)
        maxdd = max(maxdd, peak - e)
    print(f"--- equity: start ${eqs[0]:.0f} end ${eqs[-1]:.0f} "
          f"peak ${max(eqs):.0f} max drawdown ${maxdd:.0f} "
          f"({maxdd/max(eqs):.1%} of peak) ---\n")

print("=== HYPOTHESIS TESTS (counterfactual filters on the same snipe fills) ===\n")

print("H1: tighten max_edge veto (live edge>=0.20 band looked toxic)")
summary("  current (edge<=0.25 implied)", snipe)
summary("  edge<=0.20", [r for r in snipe if (r["edge"] or 0) <= 0.20])
summary("  edge<=0.18", [r for r in snipe if (r["edge"] or 0) <= 0.18])
summary("  edge 0.20-0.25 (the suspect band)",
        [r for r in snipe if 0.20 < (r["edge"] or 0) <= 0.25])
print()

print("H2: restrict by market kind (5m resolves on Chainlink, basis risk)")
for k in ["5m", "15m", "1h"]:
    summary(f"  {k} only", [r for r in snipe if r["kind"] == k])
summary("  15m+1h only (drop 5m)", [r for r in snipe if r["kind"] in ("15m", "1h")])
print()

print("H3: lower max_ask from 0.80")
summary("  ask<=0.70", [r for r in snipe if r["px"] <= 0.70])
summary("  ask<=0.65", [r for r in snipe if r["px"] <= 0.65])
summary("  ask 0.70-0.80 (suspect band)", [r for r in snipe if 0.70 < r["px"] <= 0.80])
print()

print("H4: minimum size floor (tiny fills add noise/cost)")
summary("  cost>=$10", [r for r in snipe if r["cost"] >= 10])
summary("  cost<$10 (suspect)", [r for r in snipe if r["cost"] < 10])
print()

print("H5: combined candidate (edge<=0.20 AND ask<=0.70 AND cost>=$10)")
summary("  combined", [r for r in snipe
                       if (r["edge"] or 0) <= 0.20 and r["px"] <= 0.70 and r["cost"] >= 10])
