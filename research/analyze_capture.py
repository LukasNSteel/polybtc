"""Does capture (FAK fill) rate fall on the highest-edge snipes?

For each snipe attempt we infer outcome (filled vs 'paper FAK killed: lost the
race') and bucket the fill rate by model edge and by ask price. If the biggest
edges fill *less* often, the live haircut is worse than a uniform discount,
because those windows are also where most of the PnL lives.

Usage: python research/analyze_capture.py [logs/session_*.log]
"""
import glob
import re
import sys
from collections import defaultdict
from datetime import datetime

paths = sys.argv[1:] or [sorted(glob.glob("logs/session_*.log"))[-1]]

SNIPE_RE = re.compile(
    r"^(?P<ts>[\d-]+ [\d:,]+) \S+\s+INFO\s+SNIPE (?P<title>.+?) (?P<side>UP|DN): "
    r"ask (?P<ask>[\d.]+) \+ fee [\d.]+ vs robust [\d.]+ "
    r"\(blend [\d.]+, edge (?P<edge>[\d.]+), \$(?P<usd>[\d.]+)\)")
FILL_RE = re.compile(
    r"^(?P<ts>[\d-]+ [\d:,]+) \S+\s+INFO\s+FILL (?P<side>UP|DN)\s+snipe\s+"
    r"(?P<title>.+?)\s+[\d.]+ sh @ (?P<px>[\d.]+)")
KILL_RE = re.compile(
    r"^(?P<ts>[\d-]+ [\d:,]+) \S+\s+INFO\s+paper FAK killed: (?P<title>.+?) "
    r"(?P<side>UP|DN), ask gone from (?P<px>[\d.]+)")


def ts(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S,%f")


snipes, events = [], []  # events: (time, title, side, kind) kind in {fill,kill}
for path in paths:
    with open(path) as f:
        for line in f:
            m = SNIPE_RE.match(line)
            if m:
                d = m.groupdict()
                snipes.append((ts(d["ts"]), d))
                continue
            m = FILL_RE.match(line)
            if m:
                events.append((ts(m["ts"]), m["title"], m["side"], "fill"))
                continue
            m = KILL_RE.match(line)
            if m:
                events.append((ts(m["ts"]), m["title"], m["side"], "kill"))

# Each event consumes the most recent unconsumed snipe intent (same title+side).
snipes.sort(key=lambda x: x[0])
events.sort(key=lambda x: x[0])
used = [False] * len(snipes)
attempts = []  # (edge, ask, outcome)
for et, title, side, kind in events:
    best = None
    for i, (st, d) in enumerate(snipes):
        if used[i] or st > et:
            continue
        if d["title"] == title and d["side"] == side:
            if best is None or st > snipes[best][0]:
                best = i
    if best is not None:
        used[best] = True
        d = snipes[best][1]
        attempts.append((float(d["edge"]), float(d["ask"]), kind))

n_fill = sum(1 for *_, k in attempts if k == "fill")
n_kill = sum(1 for *_, k in attempts if k == "kill")
print(f"=== {paths[-1].split('/')[-1]} ===")
print(f"matched attempts: {len(attempts)}  fill={n_fill}  kill={n_kill}  "
      f"overall capture={n_fill/(n_fill+n_kill):.0%}\n")


def bucket(name, keyfn):
    g = defaultdict(lambda: [0, 0])
    for edge, ask, k in attempts:
        g[keyfn(edge, ask)][0 if k == "fill" else 1] += 1
    print(f"--- capture by {name} ---")
    print(f"{'bucket':>12} {'fills':>6} {'kills':>6} {'capture%':>9}")
    for key in sorted(g):
        fl, kl = g[key]
        print(f"{str(key):>12} {fl:>6} {kl:>6} {fl/(fl+kl):>8.0%}")
    print()


bucket("edge", lambda e, a: f"{int(e*20)/20:.2f}+")
bucket("ask price", lambda e, a: f"{int(a*10)/10:.1f}-{int(a*10)/10+0.1:.1f}")
