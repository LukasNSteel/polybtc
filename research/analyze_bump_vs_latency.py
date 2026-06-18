"""Quantify, on REAL snipe decisions, how much P(up) drift comes from the
250ms uncancellable speed bump vs the ~20ms of latency an AWS VPC proxy could
shave — and how that compares to each trade's actual edge.

Key identity (Bachelier/Gaussian fair value, P = Phi(d), d = ln(S/K)/sig_tau):
    dP/d(lnS) = phi(d) / sig_tau,   sig_tau = vol_per_sec * sqrt(tau)
    1-sigma price drift over window T (log) = vol_per_sec * sqrt(T)
  => 1-sigma P swing over T  =  phi(d) * sqrt(T / tau)
VOLATILITY CANCELS. We only need P (~the ask) and tau (time to resolution).

We read SNIPE decision lines from a session log, derive tau from the market
window end in the title (ET -> local = ET + 14h in June/AEST), and compare:
    dP_20ms  (best case an AWS Tokyo proxy could buy back)
    dP_bump  (the 250ms uncancellable hold you eat no matter what)
against the trade's own edge.

Usage: python research/analyze_bump_vs_latency.py [logfile]
"""

import math
import re
import statistics
import sys
from datetime import datetime, timedelta

LOG = sys.argv[1] if len(sys.argv) > 1 else "logs/session_1781421149.log"
ET_TO_LOCAL_HOURS = 14  # EDT (UTC-4) -> AEST (UTC+10)
BUMP_S = 0.250
LAT_S = 0.020   # ~20ms one-way, the realistic AWS-VPC saving

SNIPE_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ .*?SNIPE (?P<title>.+?) "
    r"(?P<side>UP|DN): ask (?P<ask>[\d.]+).*?edge (?P<edge>-?[\d.]+),")
SETTLE_RE = re.compile(r"SETTLE (?P<title>.+?) -> (?P<out>UP|DOWN) \|")
# window end time, e.g. "...3:10AM-3:15AM ET" -> grab the 3:15AM
END_RE = re.compile(r"(\d{1,2})(?::(\d{2}))?\s*([AP]M)-(\d{1,2})(?::(\d{2}))?\s*([AP]M)\s*ET")
DATE_RE = re.compile(r"([A-Z][a-z]+ \d{1,2})")
MONTHS = {m: i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"], 1)}


def parse_close_local(title: str, year: int):
    em = END_RE.search(title)
    dm = DATE_RE.search(title)
    if not em or not dm:
        return None
    hh, mm, ap = int(em.group(4)), int(em.group(5) or 0), em.group(6)
    if ap == "PM" and hh != 12:
        hh += 12
    elif ap == "AM" and hh == 12:
        hh = 0
    mon_name, day = dm.group(1).split()
    mon = MONTHS.get(mon_name)
    if mon is None:
        return None
    et = datetime(year, mon, int(day), hh, mm)
    return et + timedelta(hours=ET_TO_LOCAL_HOURS)


# normal pdf / inverse cdf (Acklam approximation) — no scipy dependency
def norm_pdf(x):
    return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)


def norm_ppf(p):
    p = min(max(p, 1e-9), 1 - 1e-9)
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    plow, phigh = 0.02425, 1 - 0.02425
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
               ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    if p > phigh:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
                ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    q = p - 0.5
    r = q * q
    return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5]) * q / \
           (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)


def pct(xs, q):
    if not xs:
        return float("nan")
    xs = sorted(xs)
    i = min(len(xs) - 1, int(q * len(xs)))
    return xs[i]


def main():
    year = 2026
    settles = {}
    rows = []
    with open(LOG) as f:
        for line in f:
            sm = SETTLE_RE.search(line)
            if sm:
                settles[sm.group("title")] = sm.group("out")
                continue
            m = SNIPE_RE.search(line)
            if not m:
                continue
            ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
            close = parse_close_local(m.group("title"), year)
            if close is None:
                continue
            tau = (close - ts).total_seconds()
            if tau <= 0 or tau > 4 * 3600:
                continue
            P = float(m.group("ask"))            # market-implied prob of this side
            edge = float(m.group("edge"))
            d = norm_ppf(P)
            phi = norm_pdf(d)
            dP_lat = phi * math.sqrt(LAT_S / tau)
            dP_bump = phi * math.sqrt(BUMP_S / tau)
            rows.append(dict(title=m.group("title"), side=m.group("side"),
                             tau=tau, P=P, edge=edge,
                             dP_lat=dP_lat, dP_bump=dP_bump))

    if not rows:
        print("no parseable SNIPE rows")
        return

    taus = [r["tau"] for r in rows]
    lat = [r["dP_lat"] for r in rows]
    bump = [r["dP_bump"] for r in rows]
    edges = [r["edge"] for r in rows]
    # fraction of edge consumed by each adverse swing
    bump_frac = [r["dP_bump"] / r["edge"] for r in rows if r["edge"] > 0]
    lat_frac = [r["dP_lat"] / r["edge"] for r in rows if r["edge"] > 0]
    bump_over_edge = sum(1 for r in rows if r["edge"] > 0 and r["dP_bump"] >= r["edge"])
    lat_over_edge = sum(1 for r in rows if r["edge"] > 0 and r["dP_lat"] >= r["edge"])

    print(f"log: {LOG}")
    print(f"parsed SNIPE decisions: {len(rows)}  (settles seen: {len(settles)})")
    print(f"tau (s):   median {statistics.median(taus):6.0f}  "
          f"p10 {pct(taus,0.10):5.0f}  min {min(taus):4.0f}")
    print()
    print("Adverse P(up) swing per trade (1-sigma, pp = percentage points):")
    print(f"  250ms BUMP  (unavoidable): median {100*statistics.median(bump):5.2f}pp"
          f"  p90 {100*pct(bump,0.90):5.2f}pp  max {100*max(bump):5.2f}pp")
    print(f"  20ms LATENCY (AWS can buy): median {100*statistics.median(lat):5.2f}pp"
          f"  p90 {100*pct(lat,0.90):5.2f}pp  max {100*max(lat):5.2f}pp")
    print(f"  ratio bump/latency = sqrt(250/20) = {math.sqrt(BUMP_S/LAT_S):.2f}x  "
          f"(constant, vol- and trade-independent)")
    print()
    print(f"edge: median {100*statistics.median(edges):.2f}pp")
    print(f"  bump swing as % of edge:    median {100*statistics.median(bump_frac):5.1f}%"
          f"  p90 {100*pct(bump_frac,0.90):6.1f}%")
    print(f"  latency swing as % of edge: median {100*statistics.median(lat_frac):5.1f}%"
          f"  p90 {100*pct(lat_frac,0.90):6.1f}%")
    print()
    print(f"trades where 1-sigma BUMP drift alone >= entire edge: "
          f"{bump_over_edge}/{len(bump_frac)} ({100*bump_over_edge/len(bump_frac):.0f}%)")
    print(f"trades where 1-sigma 20ms latency drift >= entire edge: "
          f"{lat_over_edge}/{len(bump_frac)} ({100*lat_over_edge/len(bump_frac):.0f}%)")

    # win rate by bump-exposure bucket (filled snipes that we can settle)
    won = lost = unk = 0
    hi_won = hi_tot = lo_won = lo_tot = 0
    med_bump = statistics.median(bump)
    for r in rows:
        out = settles.get(r["title"])
        if out is None:
            unk += 1
            continue
        win = (out == "UP" and r["side"] == "UP") or (out == "DOWN" and r["side"] == "DN")
        won += win
        lost += (not win)
        if r["dP_bump"] >= med_bump:
            hi_tot += 1
            hi_won += win
        else:
            lo_tot += 1
            lo_won += win
    print()
    print(f"settled snipe decisions: won {won}, lost {lost} "
          f"(win rate {100*won/max(won+lost,1):.0f}%), unmatched {unk}")
    if hi_tot and lo_tot:
        print(f"  win rate, HIGH bump-exposure half: {100*hi_won/hi_tot:.0f}% ({hi_tot})")
        print(f"  win rate, LOW  bump-exposure half: {100*lo_won/lo_tot:.0f}% ({lo_tot})")


if __name__ == "__main__":
    main()
