"""Grid search over DIR x EDGE x DIST thresholds to maximize snipe PnL.

Same methodology as the pasted analysis: across every settled snipe FILL, test
every combination of a direction filter (all / up / dn), an EDGE floor, and a
DIST window (floor + ceiling), and report the rule sets that maximize total PnL,
that give a 100% win rate, the per-direction sweet spot, and the DIST-only rule.

Trade universe = real snipe fills (shadow_taker.jsonl `filled`) joined to:
  * EDGE  -> the net edge from the matching SNIPE fire line in the session logs;
  * DIST  -> dist_sigma recorded on the fill;
  * outcome -> the SETTLE line for that market (win = our side resolved).
Per-trade PnL is computed from the actual fill (shares, avg fill px) and the
binary outcome, so a loss == the risk staked (matches the pasted structure).

Run:  .venv/bin/python -m research.grid_search_filters
"""
import json
import re
import datetime
from glob import glob
from itertools import product

SETTLE = re.compile(r"SETTLE (.+?) -> (UP|DOWN) ")
SNIPE = re.compile(
    r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+ .*SNIPE (.+?) (UP|DN): ask "
    r"[\d.]+ \(limit [\d.]+\) \+ fee [\d.]+ vs robust [\d.]+ "
    r"\(blend [\d.]+, edge ([\d.]+),")


def parse_ts(s):
    return datetime.datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(
        tzinfo=datetime.timezone.utc).timestamp()


def load_logs():
    settles, snipes = {}, []
    for path in glob("logs/session_*.log"):
        for line in open(path, errors="ignore"):
            ms = SETTLE.search(line)
            if ms:
                settles[ms.group(1).strip()] = "up" if ms.group(2) == "UP" else "dn"
            mf = SNIPE.search(line)
            if mf:
                snipes.append({"ts": parse_ts(mf.group(1)),
                               "title": mf.group(2).strip(),
                               "side": mf.group(3).lower(),
                               "edge": float(mf.group(4))})
    return settles, snipes


def match_edge(fill, snipes):
    best, bestdt = None, 15.0
    for s in snipes:
        if s["title"] == fill["title"] and s["side"] == fill["side"]:
            dt = abs(s["ts"] - fill["ts"])
            if dt < bestdt:
                best, bestdt = s["edge"], dt
    return best


def fee_per_share(px):
    return 0.07 * (px * (1 - px))


def build_trades():
    settles, snipes = load_logs()
    trades = []
    for line in open("logs/shadow_taker.jsonl", errors="ignore"):
        try:
            r = json.loads(line)
        except Exception:
            continue
        if (r.get("type") != "attempt" or not r.get("filled")
                or r.get("leg") != "snipe"):
            continue
        outc = settles.get((r.get("title") or "").strip())
        dist = r.get("dist_sigma")
        sh, px = r.get("filled_shares"), r.get("avg_fill_px")
        if outc is None or dist is None or not sh or not px:
            continue
        edge = match_edge(r, snipes)
        if edge is None:
            continue
        won = r["side"] == outc
        fee = sh * fee_per_share(px)
        risk = sh * px + fee
        pnl = (sh * (1 - px) - fee) if won else -(sh * px + fee)
        trades.append({"dir": r["side"], "edge": edge, "dist": dist,
                       "won": won, "pnl": pnl, "risk": risk,
                       "kind": r.get("kind")})
    return trades


def subset(trades, dirf, emin, dlo, dhi):
    out = []
    for t in trades:
        if dirf != "all" and t["dir"] != dirf:
            continue
        if t["edge"] < emin or not (dlo <= t["dist"] <= dhi):
            continue
        out.append(t)
    return out


def summ(rows):
    n = len(rows)
    w = sum(t["won"] for t in rows)
    pnl = sum(t["pnl"] for t in rows)
    return n, w, pnl


def report(label, rows, rule):
    n, w, pnl = summ(rows)
    wr = f"{w / n * 100:.1f}%" if n else "-"
    print(f"  {label}")
    print(f"    rule: {rule}")
    print(f"    trades {n}  win {wr} (W{w}/{n - w})  total PnL {pnl:+.2f}\n")


def main():
    trades = build_trades()
    n, w, pnl = summ(trades)
    kinds = {}
    for t in trades:
        kinds[t["kind"]] = kinds.get(t["kind"], 0) + 1
    print(f"settled snipe fills: {n}  (kinds: {kinds})")
    print(f"BASELINE (all trades): win {w / n * 100:.1f}%  total PnL {pnl:+.2f}\n"
          if n else "no trades")
    if n < 4:
        print("too few trades to grid-search meaningfully.")
        return

    edges = sorted({round(t["edge"], 3) for t in trades})
    dists = sorted({round(t["dist"], 2) for t in trades})
    egrid = [-1.0] + edges
    dlo_grid = [-1.0] + dists
    dhi_grid = dists + [99.0]
    MIN_N = 4

    results = []
    for dirf, emin, dlo, dhi in product(("all", "up", "dn"), egrid,
                                        dlo_grid, dhi_grid):
        if dlo > dhi:
            continue
        rows = subset(trades, dirf, emin, dlo, dhi)
        if len(rows) < MIN_N:
            continue
        rn, rw, rp = summ(rows)
        results.append((rp, rn, rw, dirf, emin, dlo, dhi, rows))

    def rule_str(dirf, emin, dlo, dhi):
        parts = []
        if dirf != "all":
            parts.append(f"DIR == {dirf.upper()}")
        if emin > -1.0:
            parts.append(f"EDGE >= {emin:.3f}")
        if dlo > -1.0:
            parts.append(f"DIST >= {dlo:.2f}")
        if dhi < 99.0:
            parts.append(f"DIST <= {dhi:.2f}")
        return " AND ".join(parts) or "no constraint"

    print("=" * 64)
    best = max(results, key=lambda x: x[0])
    report("ABSOLUTE MAXIMIZER (highest total PnL, n>=4)",
           best[7], rule_str(*best[3:7]))

    zero = [r for r in results if r[2] == r[1]]  # wins == trades
    if zero:
        bz = max(zero, key=lambda x: x[0])
        report("ZERO-LOSS (100% win rate, max PnL among them, n>=4)",
               bz[7], rule_str(*bz[3:7]))

    for d in ("up", "dn"):
        sub = [r for r in results if r[3] == d]
        if sub:
            bd = max(sub, key=lambda x: x[0])
            report(f"BEST {d.upper()}-ONLY rule", bd[7], rule_str(*bd[3:7]))

    donly = [r for r in results if r[3] == "all" and r[4] <= -1.0]
    if donly:
        bdo = max(donly, key=lambda x: x[0])
        report("DIST-ONLY (ignore EDGE)", bdo[7], rule_str(*bdo[3:7]))

    print("=" * 64)
    print("LOSS STRUCTURE — where the losers sit (edge x dist of losses):")
    losers = [t for t in trades if not t["won"]]
    for t in sorted(losers, key=lambda t: t["edge"]):
        print(f"    L  DIR {t['dir'].upper()}  EDGE {t['edge']:.3f}  "
              f"DIST {t['dist']:.2f}  pnl {t['pnl']:+.2f}")


if __name__ == "__main__":
    main()
