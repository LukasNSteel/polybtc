"""Ad-hoc breakdown of two paper sessions: 1000-cap (965) vs 250-cap (954)."""
import re
import sys
from collections import defaultdict

SETTLE = re.compile(
    r"SETTLE (?P<title>.+?) -> (?P<res>UP|DOWN) \| payout \$(?P<payout>[\d.]+) "
    r"cost \$(?P<cost>[\d.]+) pnl \$(?P<pnl>[+\-][\d.]+)"
)
FILL = re.compile(
    r"FILL (?P<side>UP|DN)\s+(?P<leg>\w+)\s+(?P<title>.+?) (?P<sh>[\d.]+) sh @ "
    r"(?P<px>[\d.]+) \(\$(?P<cost>[\d.]+)(?: \+fee (?P<fee>[\d.]+))?\)"
)
RACE = re.compile(r"paper FAK lost the race")
EQ = re.compile(r"equity \$(?P<eq>[\d.]+)")


def interval(title: str) -> str:
    # 5-minute windows look like "11:25PM-11:30PM", 15m "11:15PM-11:30PM",
    # hourly "11PM ET", 4h "8:00PM-12:00AM".
    m = re.search(r"(\d+):(\d+)[AP]M-(\d+):(\d+)[AP]M", title)
    if m:
        h1, m1, h2, m2 = map(int, m.groups())
        span = ((h2 * 60 + m2) - (h1 * 60 + m1)) % (24 * 60)
        if span in (5,):
            return "5m"
        if span in (15,):
            return "15m"
        if span in (60,):
            return "1h"
        if span in (240,):
            return "4h"
        return f"{span}m"
    if re.search(r"\d+(?::\d+)?[AP]M ET$", title):
        return "1h"
    return "?"


def analyze(path: str, label: str):
    settles, fills, races = [], [], 0
    last_eq = None
    fee_total = 0.0
    with open(path) as f:
        for line in f:
            if (m := SETTLE.search(line)):
                settles.append((m["title"], m["res"], float(m["payout"]),
                                float(m["cost"]), float(m["pnl"])))
            elif (m := FILL.search(line)):
                fee = float(m["fee"]) if m["fee"] else 0.0
                fee_total += fee
                fills.append((m["side"], m["leg"], m["title"], float(m["sh"]),
                              float(m["px"]), float(m["cost"]), fee))
            elif RACE.search(line):
                races += 1
            if (m := EQ.search(line)):
                last_eq = float(m["eq"])

    wins = [s for s in settles if s[2] > 0]
    losses = [s for s in settles if s[2] == 0]
    win_pnl = sum(s[4] for s in wins)
    loss_pnl = sum(s[4] for s in losses)
    net = win_pnl + loss_pnl

    print("=" * 72)
    print(f"{label}   ({path.split('/')[-1]})")
    print("=" * 72)
    print(f"settlements: {len(settles)}   wins: {len(wins)}   losses: {len(losses)}"
          f"   win-rate: {len(wins)/len(settles)*100:.0f}%")
    print(f"  gross from wins : ${win_pnl:+.2f}   (avg win  ${win_pnl/max(len(wins),1):+.2f})")
    print(f"  gross from loss : ${loss_pnl:+.2f}   (avg loss ${loss_pnl/max(len(losses),1):+.2f})")
    print(f"  NET settled pnl : ${net:+.2f}   (before fees)")
    print(f"  fees paid (fills): ${fee_total:.2f}")
    print(f"  net after fees  : ${net - fee_total:+.2f}")
    print(f"  fills: {len(fills)}   FAK races lost: {races}   last equity seen: ${last_eq}")

    # avg entry price (cost-weighted) — how much of a 'favorite' are we buying?
    tot_cost = sum(f[5] for f in fills)
    wavg_px = sum(f[4] * f[5] for f in fills) / max(tot_cost, 1e-9)
    print(f"  cost-weighted avg entry price: {wavg_px:.3f}   total fill cost: ${tot_cost:.2f}")

    # breakdown by interval
    print("\n  by market interval:")
    agg = defaultdict(lambda: [0, 0, 0.0])  # wins, losses, net
    for s in settles:
        iv = interval(s[0])
        agg[iv][0] += s[2] > 0
        agg[iv][1] += s[2] == 0
        agg[iv][2] += s[4]
    for iv in sorted(agg):
        w, l, n = agg[iv]
        print(f"    {iv:>4}: {w:2}W {l:2}L  net ${n:+8.2f}")

    # breakdown by resolution side
    print("\n  by winning side of position (did we hold the winner?):")
    return net, fee_total, last_eq


for label, p in [("1000-CAP", "logs/session_1781925965.log"),
                 (" 250-CAP", "logs/session_1781925954.log")]:
    analyze(p, label)
    print()
