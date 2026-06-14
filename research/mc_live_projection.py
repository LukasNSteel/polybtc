"""Monte Carlo: $1k -> $100k using empirical fill distribution + config caps."""

import glob
import json
import math
import random
import re
import statistics
from datetime import datetime

random.seed(42)

MAX_TAKE = 100
MAX_EXPOSURE = 500
KILL = 300
TARGET = 100_000
START = 1_000
MAX_DAYS = 730
N = 10_000


def load_fills(paths, min_ask=0.50):
    re_ts = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),(\d{3})")
    re_fill = re.compile(
        r"FILL (UP|DN)\s+(\w+)\s+(.+?) (\d+(?:\.\d+)?) sh @ ([\d.]+) "
        r"\(\$([\d.]+)(?: \+fee ([\d.]+))?\)"
    )
    re_settle = re.compile(r"SETTLE (.+?) -> (UP|DOWN)")
    fills = []
    settles = {}
    for path in paths:
        with open(path) as f:
            for line in f:
                m = re_ts.match(line)
                if not m:
                    continue
                if m2 := re_settle.search(line):
                    settles[m2.group(1)] = m2.group(2)
                if m2 := re_fill.search(line):
                    side, leg, title, shares, price, cost, fee = m2.groups()
                    if leg not in ("snipe", "scalp"):
                        continue
                    price = float(price)
                    if price < min_ask:
                        continue
                    fills.append(
                        (
                            title,
                            side,
                            float(shares),
                            float(cost),
                            float(fee or 0),
                        )
                    )
    pool = []
    for title, side, shares, cost, fee in fills:
        st = settles.get(title)
        if not st:
            continue
        win = st == ("UP" if side == "UP" else "DOWN")
        dep = cost + fee
        net = (shares if win else 0) - cost - fee
        pool.append((dep, net))
    return pool


def poisson(lam):
    if lam <= 0:
        return 0
    l = math.exp(-lam)
    k = 0
    p = 1.0
    while p > l:
        k += 1
        p *= random.random()
    return k - 1


def simulate(pool, pnl_scale, fill_rate_scale, fills_per_day):
    nets = [n * pnl_scale for _, n in pool]
    deps = [max(5.0, d) for d, _ in pool]
    lam = fills_per_day * fill_rate_scale

    eq = START
    session_start = START
    cash = START
    kills = 0
    days = 0
    milestones = [2000, 5000, 10000, 25000, 50000, 100000]
    hit = {m: None for m in milestones}

    while days < MAX_DAYS and eq < TARGET:
        day_deploy = 0.0
        day_pnl = 0.0
        for _ in range(poisson(lam)):
            j = random.randrange(len(nets))
            dep = min(deps[j], MAX_TAKE, max(0.0, MAX_EXPOSURE - day_deploy), cash - day_deploy)
            if dep < 5:
                continue
            sc = dep / deps[j]
            day_pnl += nets[j] * sc
            day_deploy += dep
        cash = cash - day_deploy + day_deploy + day_pnl
        eq = cash
        days += 1

        if eq <= session_start - KILL:
            kills += 1
            session_start = eq
            if eq < 200:
                break

        for m in milestones:
            if hit[m] is None and eq >= m:
                hit[m] = days

    return {
        "reached": eq >= TARGET,
        "kills": kills,
        "days": days if eq >= TARGET else None,
        "end_eq": eq,
        "hit": hit,
    }


def summarize(label, pool, pnl_scale, fill_rate_scale, fills_per_day):
    paths = [simulate(pool, pnl_scale, fill_rate_scale, fills_per_day) for _ in range(N)]
    reach = [p for p in paths if p["reached"]]
    out = {
        "label": label,
        "p100k": len(reach) / N,
        "p_kill": sum(p["kills"] > 0 for p in paths) / N,
        "avg_kills": statistics.mean(p["kills"] for p in paths),
        "median_end": statistics.median(p["end_eq"] for p in paths),
    }
    if reach:
        days = [p["days"] for p in reach]
        out["days_p50"] = statistics.median(days)
        out["days_p75"] = statistics.quantiles(days, n=4)[2]
        out["days_p90"] = sorted(days)[int(len(days) * 0.9)]
    out["milestones"] = {}
    for m in [2000, 5000, 10000, 25000, 50000, 100000]:
        sub = [p["hit"][m] for p in paths if p["hit"][m] is not None]
        out["milestones"][m] = {
            "p": len(sub) / N,
            "p50": statistics.median(sub) if sub else None,
        }
    return out


def main():
    fills_best = load_fills(["logs/session_1781273413.log"])
    fills_all = load_fills(glob.glob("logs/session_*.log"))
    fpd_best = len(fills_best) / 35.6
    fpd_all = len(fills_all) / 50.0  # rough span across all sessions

    print(
        f"pool best={len(fills_best)} avg_net=${statistics.mean(n for _, n in fills_best):.2f} "
        f"win={100*sum(1 for _,n in fills_best if n>0)/len(fills_best):.1f}% "
        f"fills/day~{fpd_best:.0f}"
    )
    print(
        f"pool all={len(fills_all)} avg_net=${statistics.mean(n for _, n in fills_all):.2f} "
        f"win={100*sum(1 for _,n in fills_all if n>0)/len(fills_all):.1f}%"
    )

    scenarios = [
        ("best_live70", "Best session · live 70%", fills_best, 0.70, 0.85, fpd_best),
        ("best_live50", "Best session · live 50%", fills_best, 0.50, 0.75, fpd_best),
        ("best_paper", "Best session · paper", fills_best, 1.00, 1.00, fpd_best),
        ("all_live70", "All sessions · live 70%", fills_all, 0.70, 0.85, fpd_all),
        ("all_live50", "All sessions · live 50%", fills_all, 0.50, 0.75, fpd_all),
    ]

    results = {}
    for key, label, pool, ps, fr, fpd in scenarios:
        r = summarize(label, pool, ps, fr, fpd)
        results[key] = r
        print(f"\n{label}")
        print(f"  P($100k in 2yr): {r['p100k']*100:.1f}%")
        print(f"  P(at least one kill): {r['p_kill']*100:.1f}%  avg kills: {r['avg_kills']:.1f}")
        if "days_p50" in r:
            print(
                f"  Days to $100k if reached: p50={r['days_p50']:.0f}  "
                f"p75={r['days_p75']:.0f}  p90={r['days_p90']:.0f}"
            )
        print(f"  Median ending equity @2yr: ${r['median_end']:,.0f}")
        for m, mm in r["milestones"].items():
            if mm["p50"]:
                print(f"    P(${m:,})={mm['p']*100:.1f}%  p50={mm['p50']:.0f}d")

    print("\nJSON")
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
