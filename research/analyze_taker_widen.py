"""Model the effect of widening taker limit tolerance.

Parses 'paper FAK rejected' lines from a session log. Each reject records the
post-hold ask, our limit (== signal ask_px, zero slack), and the fair (mid)
drift during the hold. A reject only fires because ask > limit. If we had
widened the limit by N ticks, the order would have filled iff (ask - limit) <=
N*tick. We also approximate whether the trade is still +EV at fill: because the
whole book richened (mid moved up by `drift`), the edge erosion from paying the
higher ask is roughly (gap - drift).

Usage: python research/analyze_taker_widen.py logs/session_XXXX.log
"""
import re
import sys
from collections import Counter

TICK = 0.01
SNIPER_MIN_EDGE = 0.10  # config: sniper.min_edge (robust)

REJECT_RE = re.compile(
    r"paper FAK rejected:.* (?:UP|DN), ask (\S+) vs limit ([\d.]+) "
    r"after [\d]+ms hold \(fair drift ([+-][\d.]+)"
)
RACE_RE = re.compile(r"paper FAK lost the race")
CONTEST_RE = re.compile(r"paper FAK fully contested")
ADVERSE_RE = re.compile(r"paper ADVERSE FILL")
FILL_LINE_RE = re.compile(r"paper (PARTIAL FILL|ADVERSE FILL)")


def main(path: str) -> None:
    rejects = []          # (gap, drift) for finite-ask rejects
    gone = 0              # ask vanished entirely (unrecoverable)
    races = contests = adverse = 0
    placements = 0        # SNIPE/SCALP order placements (~= attempts)

    with open(path) as f:
        for line in f:
            if " SNIPE " in line or " SCALP " in line:
                placements += 1
            if RACE_RE.search(line):
                races += 1
                continue
            if CONTEST_RE.search(line):
                contests += 1
                continue
            if ADVERSE_RE.search(line):
                adverse += 1
            m = REJECT_RE.search(line)
            if not m:
                continue
            ask_s, limit_s, drift_s = m.groups()
            limit = float(limit_s)
            drift = float(drift_s)
            if ask_s == "gone":
                gone += 1
                continue
            ask = float(ask_s)
            gap = ask - limit
            if gap <= 0:
                # rejected but ask not above limit (rounding / vanished depth)
                gone += 1
                continue
            rejects.append((gap, drift))

    n_rej = len(rejects)
    kills = n_rej + gone + races + contests
    fills_est = max(0, placements - kills)
    cur_rate = (100 * fills_est / placements) if placements else 0.0
    print(f"log: {path}")
    print(f"rejects parsed: finite-ask {n_rej}, gone/vanished {gone}, "
          f"lost-race {races}, fully-contested {contests}, adverse-fills {adverse}")
    print(f"placements (attempts) {placements}, kills {kills}, "
          f"est fills {fills_est}  ->  session fill rate ~{cur_rate:.0f}%")
    print()

    if not n_rej:
        return

    # distribution of how far the ask richened past our (zero-slack) limit
    gap_ticks = Counter(round(g / TICK) for g, _ in rejects)
    print("how far the ask richened past our limit (in ticks):")
    for t in sorted(gap_ticks):
        print(f"  +{t} tick: {gap_ticks[t]:3d}  ({100*gap_ticks[t]/n_rej:4.0f}%)")
    print()

    print(f"{'widen':>6} | {'recovered fills':>16} | {'avg extra ¢/sh':>14} | "
          f"{'still +EV*':>10} | projected")
    print("-" * 80)
    for n in (1, 2, 3):
        thresh = n * TICK + 1e-9
        hit = [(g, d) for g, d in rejects if g <= thresh]
        if not hit:
            print(f"  +{n}t  | {'0':>16} | {'-':>14} | {'-':>10}")
            continue
        avg_gap_c = 100 * sum(g for g, _ in hit) / len(hit)
        # +EV proxy: edge erosion = gap - drift (book richened with us);
        # still +EV if min_edge(0.10) - max(0, gap - drift) > 0
        ev_ok = sum(1 for g, d in hit if (SNIPER_MIN_EDGE - max(0.0, g - d)) > 0)
        pct = 100 * len(hit) / n_rej
        # net new fills after ~20% still lose the race on the recovered quotes
        net_new = len(hit) * 0.8
        proj_rate = (100 * (fills_est + net_new) / placements) if placements else 0
        print(f"  +{n}t  | {len(hit):>4} / {n_rej} ({pct:3.0f}%) | "
              f"{avg_gap_c:>13.1f} | {100*ev_ok/len(hit):>8.0f}% | "
              f"fill rate ~{cur_rate:.0f}%->{proj_rate:.0f}%")

    print()
    # how often the ask richening just tracked the fair drift (edge preserved)
    tracked = sum(1 for g, d in rejects if d > 0 and abs(g - d) <= TICK)
    favourable = sum(1 for g, d in rejects if d > 0)
    print(f"rejects where fair drifted UP (our fair richened too): "
          f"{favourable}/{n_rej} ({100*favourable/n_rej:.0f}%)")
    print(f"  of those, ask richening tracked drift within 1 tick "
          f"(edge ~preserved): {tracked}")
    print()
    print("* +EV proxy assumes edge erosion = max(0, gap - drift) against the "
          "0.10 robust min_edge; rough, ignores fees/contention.")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "logs/session_1781839742.log")
