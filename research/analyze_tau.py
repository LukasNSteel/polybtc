"""Cross-tab snipe fills by time-to-expiry (tau) at fill, joined to settlement.
Local log time = ET + 14h (verified against settle timestamps).
"""

import glob
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta

paths = sys.argv[1:] or sorted(glob.glob("logs/session_*.log"))

FILL_RE = re.compile(
    r"^(?P<ts>[\d-]+ [\d:,]+) \S+\s+INFO\s+FILL (?P<side>UP|DN)\s+(?P<leg>\w+) "
    r"(?P<title>.+?)\s+(?P<sh>[\d.]+) sh @ (?P<px>[\d.]+) \(\$(?P<cost>[\d.]+)(?: \+fee (?P<fee>[\d.]+))?\)"
)
SETTLE_RE = re.compile(
    r"^(?P<ts>[\d-]+ [\d:,]+) \S+\s+INFO\s+SETTLE (?P<title>.+?) -> (?P<out>UP|DOWN) "
    r"\| payout \$(?P<pay>[\d.]+) cost \$(?P<cost>[\d.]+) pnl \$(?P<pnl>[+-][\d.]+)"
)


def parse_ts(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S,%f")


def close_dt(title, fill_dt):
    """Market close in local log time (ET + 14h)."""
    m = re.search(r"(\d+):(\d+)(AM|PM)-(\d+):(\d+)(AM|PM)", title)
    if m:
        h1, m1, ap1, h2, m2, ap2 = m.groups()
        h, mm, ap = int(h2), int(m2), ap2
    else:
        m = re.search(r"(\d+)(AM|PM) ET", title)
        if not m:
            return None
        h, mm, ap = int(m.group(1)), 0, m.group(2)
        # hourly market titled by its open hour; closes one hour later
        hh = (h % 12 + (12 if ap == "PM" else 0)) + 1
        et = fill_dt.replace(hour=0, minute=0, second=0, microsecond=0)
        c = et + timedelta(hours=hh + 14)
        while c < fill_dt:
            c += timedelta(days=1)
        return c
    hh = h % 12 + (12 if ap == "PM" else 0)
    et = fill_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    c = et + timedelta(hours=hh + 14, minutes=mm)
    while c < fill_dt - timedelta(hours=12):
        c += timedelta(days=1)
    return c


fills, settles = [], {}
for path in paths:
    with open(path) as f:
        for line in f:
            m = FILL_RE.match(line)
            if m:
                fills.append(m.groupdict())
                continue
            m = SETTLE_RE.match(line)
            if m:
                settles[m.group("title")] = m.groupdict()

rows = []
for f in fills:
    if f["leg"] != "snipe":
        continue
    s = settles.get(f["title"])
    if not s:
        continue
    fd = parse_ts(f["ts"])
    cd = close_dt(f["title"], fd)
    if cd is None:
        continue
    tau = (cd - fd).total_seconds()
    side = "up" if f["side"] == "UP" else "dn"
    won = (s["out"] == "UP") == (side == "up")
    sh, px, fee = float(f["sh"]), float(f["px"]), float(f["fee"] or 0)
    pnl = (sh if won else 0.0) - sh * px - fee
    kind = "1h" if "AM ET" in f["title"] or "PM ET" in f["title"] else None
    if kind is None:
        mm_ = re.search(r"(\d+):(\d+)(AM|PM)-(\d+):(\d+)(AM|PM)", f["title"])
        h1, m1, ap1, h2, m2, ap2 = mm_.groups()
        t1 = (int(h1) % 12 + (12 if ap1 == "PM" else 0)) * 60 + int(m1)
        t2 = (int(h2) % 12 + (12 if ap2 == "PM" else 0)) * 60 + int(m2)
        kind = {5: "5m", 15: "15m", 60: "1h", 240: "4h"}.get((t2 - t1) % 1440, "?")
    rows.append(dict(kind=kind, side=side, px=px, cost=sh * px, pnl=pnl, won=won, tau=tau))

print(f"{len(rows)} snipe fills with tau\n")


def table(name, keyfn, rows):
    groups = defaultdict(list)
    for r in rows:
        groups[keyfn(r)].append(r)
    print(f"--- {name} ---")
    print(f"{'group':>16} {'n':>4} {'win%':>6} {'cost$':>9} {'pnl$':>9} {'pnl/$':>7}")
    for k in sorted(groups, key=str):
        g = groups[k]
        c = sum(r["cost"] for r in g)
        p = sum(r["pnl"] for r in g)
        w = sum(r["won"] for r in g) / len(g)
        print(f"{str(k):>16} {len(g):>4} {w:>6.0%} {c:>9.2f} {p:>+9.2f} {p/c if c else 0:>+7.1%}")
    print()


def tb(t):
    if t <= 30: return "a <=30s"
    if t <= 60: return "b 30-60s"
    if t <= 120: return "c 1-2m"
    if t <= 300: return "d 2-5m"
    if t <= 900: return "e 5-15m"
    return "f >15m"


table("tau at fill", lambda r: tb(r["tau"]), rows)
late = [r for r in rows if r["tau"] <= 60]
table("tau<=60s by kind", lambda r: r["kind"], late)
table("tau<=60s by px", lambda r: f"{int(r['px']*10)/10:.1f}", late)
table("tau<=60s by fav", lambda r: "fav(ask>=0.5)" if r["px"] >= 0.5 else "dog(ask<0.5)", late)
early = [r for r in rows if r["tau"] > 60]
table("tau>60s by fav", lambda r: "fav(ask>=0.5)" if r["px"] >= 0.5 else "dog(ask<0.5)", early)
table("all: fav x side", lambda r: ("fav" if r["px"] >= 0.5 else "dog") + "/" + r["side"], rows)
table("all: fav x kind", lambda r: ("fav" if r["px"] >= 0.5 else "dog") + "/" + r["kind"], rows)
