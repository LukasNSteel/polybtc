"""Forensic analysis of session logs: join every snipe/scalp fill to its
market's settlement and break PnL down by kind, side, price, edge, and time.

Usage: python research/analyze_sessions.py [logs/session_*.log ...]
"""

import glob
import re
import sys
from collections import defaultdict
from datetime import datetime

paths = sys.argv[1:] or sorted(glob.glob("logs/session_*.log"))

SNIPE_RE = re.compile(
    r"^(?P<ts>[\d-]+ [\d:,]+) \S+\s+INFO\s+SNIPE (?P<title>.+?) (?P<side>UP|DN): "
    r"ask (?P<ask>[\d.]+) \+ fee (?P<fee>[\d.]+) vs robust (?P<robust>[\d.]+) "
    r"\(blend (?P<blend>[\d.]+), edge (?P<edge>[\d.]+), \$(?P<usd>[\d.]+)\)"
)
# NB: maker (mm) fills have no "+fee" suffix, and SETTLE cost can be negative
# after pair merges — both must stay optional or whole markets drop out of the
# join and skew the totals (this bias hid ~$580 of winning snipe fills once).
FILL_RE = re.compile(
    r"^(?P<ts>[\d-]+ [\d:,]+) \S+\s+INFO\s+FILL (?P<side>UP|DN)\s+(?P<leg>\w+)\s+"
    r"(?P<title>.+?)\s+(?P<sh>[\d.]+) sh @ (?P<px>[\d.]+) \(\$(?P<cost>[\d.]+)(?: \+fee (?P<fee>[\d.]+))?\)"
)
SETTLE_RE = re.compile(
    r"^(?P<ts>[\d-]+ [\d:,]+) \S+\s+INFO\s+SETTLE (?P<title>.+?) -> (?P<out>UP|DOWN) "
    r"\| payout \$(?P<pay>[\d.]+) cost \$(?P<cost>-?[\d.]+) pnl \$(?P<pnl>[+-][\d.]+)"
)
KILLED_RE = re.compile(r"paper FAK killed")


def parse_ts(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S,%f").timestamp()


def kind_of(title):
    m = re.search(r"(\d+):(\d+)(AM|PM)-(\d+):(\d+)(AM|PM)", title)
    if not m:
        return "1h" if re.search(r"\d+(AM|PM) ET", title) else "?"
    h1, m1, ap1, h2, m2, ap2 = m.groups()
    t1 = (int(h1) % 12 + (12 if ap1 == "PM" else 0)) * 60 + int(m1)
    t2 = (int(h2) % 12 + (12 if ap2 == "PM" else 0)) * 60 + int(m2)
    d = (t2 - t1) % (24 * 60)
    return {5: "5m", 15: "15m", 60: "1h", 240: "4h"}.get(d, f"{d}m")


fills = []       # dicts
settles = {}     # title -> (outcome, pnl)
snipes = []      # intent lines
killed = 0

for path in paths:
    with open(path) as f:
        for line in f:
            if KILLED_RE.search(line):
                killed += 1
            m = SNIPE_RE.match(line)
            if m:
                d = m.groupdict()
                snipes.append(d)
                continue
            m = FILL_RE.match(line)
            if m:
                d = m.groupdict()
                d["session"] = path
                fills.append(d)
                continue
            m = SETTLE_RE.match(line)
            if m:
                d = m.groupdict()
                settles[d["title"]] = d

# join fills to settles
rows = []
for f in fills:
    s = settles.get(f["title"])
    if not s:
        continue
    side = "up" if f["side"] == "UP" else "dn"
    won = (s["out"] == "UP") == (side == "up")
    sh, px, fee = float(f["sh"]), float(f["px"]), float(f["fee"] or 0)
    pnl = (sh * 1.0 if won else 0.0) - sh * px - fee
    # find matching snipe intent (same title+side, closest preceding ts)
    edge = robust = None
    ft = parse_ts(f["ts"])
    best = None
    for sn in snipes:
        if sn["title"] == f["title"] and sn["side"] == f["side"]:
            st = parse_ts(sn["ts"])
            if st <= ft and (best is None or st > best):
                best = st
                edge, robust = float(sn["edge"]), float(sn["robust"])
    rows.append(dict(
        title=f["title"], kind=kind_of(f["title"]), leg=f["leg"], side=side,
        px=px, sh=sh, cost=sh * px, fee=fee, won=won, pnl=pnl,
        edge=edge, robust=robust, session=f["session"], ts=ft,
    ))

print(f"{len(fills)} fills parsed, {len(rows)} joined to settlements, {killed} FAK-killed (lost race)")
total = sum(r["pnl"] for r in rows)
print(f"joined PnL ${total:+.2f} on ${sum(r['cost'] for r in rows):.2f} cost, fees ${sum(r['fee'] for r in rows):.2f}\n")


def table(name, keyfn, rows):
    groups = defaultdict(list)
    for r in rows:
        groups[keyfn(r)].append(r)
    print(f"--- by {name} ---")
    print(f"{'group':>14} {'n':>4} {'win%':>6} {'cost$':>9} {'pnl$':>9} {'pnl/$':>7}")
    for k in sorted(groups, key=str):
        g = groups[k]
        c = sum(r["cost"] for r in g)
        p = sum(r["pnl"] for r in g)
        w = sum(r["won"] for r in g) / len(g)
        print(f"{str(k):>14} {len(g):>4} {w:>6.0%} {c:>9.2f} {p:>+9.2f} {p/c if c else 0:>+7.1%}")
    print()


sn = [r for r in rows if r["leg"] == "snipe"]
table("leg", lambda r: r["leg"], rows)
table("kind (snipe)", lambda r: r["kind"], sn)
table("side (snipe)", lambda r: r["side"], sn)
table("ask px (snipe)", lambda r: f"{int(r['px']*10)/10:.1f}-{int(r['px']*10)/10+0.1:.1f}", sn)
table("edge (snipe)", lambda r: ("?" if r["edge"] is None else
      f"{int(r['edge']*20)/20:.2f}+"), sn)
table("size $ (snipe)", lambda r: "<10" if r["cost"] < 10 else "10-50" if r["cost"] < 50 else "50-100" if r["cost"] < 100 else "100+", sn)
table("session (snipe)", lambda r: r["session"].split("_")[1][:10], sn)

# biggest losers / winners
sn.sort(key=lambda r: r["pnl"])
print("--- 10 worst snipe fills ---")
for r in sn[:10]:
    print(f"  {r['pnl']:+8.2f}  {r['kind']:>3} {r['side']} @{r['px']:.2f} x{r['sh']:.0f} edge={r['edge']} robust={r['robust']} {r['title']}")
print("--- 10 best snipe fills ---")
for r in sn[-10:]:
    print(f"  {r['pnl']:+8.2f}  {r['kind']:>3} {r['side']} @{r['px']:.2f} x{r['sh']:.0f} edge={r['edge']} robust={r['robust']} {r['title']}")
