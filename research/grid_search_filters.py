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
                       "ask": round(px, 4), "won": won, "pnl": pnl,
                       "risk": risk, "kind": r.get("kind")})
    return trades


def subset(trades, dirf, emin, emax, dlo, dhi, alo, ahi):
    out = []
    for t in trades:
        if dirf != "all" and t["dir"] != dirf:
            continue
        if not (emin <= t["edge"] <= emax):
            continue
        if not (dlo <= t["dist"] <= dhi):
            continue
        if not (alo <= t["ask"] <= ahi):
            continue
        out.append(t)
    return out


def summ(rows):
    n = len(rows)
    w = sum(t["won"] for t in rows)
    pnl = sum(t["pnl"] for t in rows)
    return n, w, pnl


NEG, POS = -1e9, 1e9


def cuts(vals, lo=True):
    """~6 quantile cut-points + an open end, so floors/ceilings stay a small grid
    instead of every unique value (keeps the 7-dim search fast)."""
    import numpy as np
    qs = np.quantile(vals, [0.0, 0.2, 0.4, 0.6, 0.8] if lo
                     else [0.2, 0.4, 0.6, 0.8, 1.0])
    pts = sorted({round(float(q), 3) for q in qs})
    return ([NEG] + pts) if lo else (pts + [POS])


def rule_str(dirf, emin, emax, dlo, dhi, alo, ahi):
    parts = []
    if dirf != "all":
        parts.append(f"DIR=={dirf.upper()}")
    if emin > NEG:
        parts.append(f"EDGE>={emin:.3f}")
    if emax < POS:
        parts.append(f"EDGE<={emax:.3f}")
    if dlo > NEG:
        parts.append(f"DIST>={dlo:.2f}")
    if dhi < POS:
        parts.append(f"DIST<={dhi:.2f}")
    if alo > NEG:
        parts.append(f"ASK>={alo:.2f}")
    if ahi < POS:
        parts.append(f"ASK<={ahi:.2f}")
    return " AND ".join(parts) or "no constraint"


def report(label, r):
    rp, rn, rw, *rule = r
    wr = f"{rw / rn * 100:.1f}%" if rn else "-"
    print(f"  {label}")
    print(f"    rule: {rule_str(*rule)}")
    print(f"    trades {rn}  win {wr} (W{rw}/{rn - rw})  total PnL {rp:+.2f}\n")


def main():
    trades = build_trades()
    n, w, pnl = summ(trades)
    kinds = {}
    for t in trades:
        kinds[t["kind"]] = kinds.get(t["kind"], 0) + 1
    print(f"settled snipe fills: {n}  (kinds: {kinds})")
    if n < 4:
        print("too few trades to grid-search meaningfully.")
        return
    print(f"BASELINE (all trades): win {w / n * 100:.1f}%  total PnL {pnl:+.2f}\n")

    E = [t["edge"] for t in trades]
    D = [t["dist"] for t in trades]
    A = [t["ask"] for t in trades]
    e_lo, e_hi = cuts(E), cuts(E, lo=False)
    d_lo, d_hi = cuts(D), cuts(D, lo=False)
    a_lo, a_hi = cuts(A), cuts(A, lo=False)
    MIN_N = 4

    results = []
    for dirf in ("all", "up", "dn"):
        for emin, emax in product(e_lo, e_hi):
            if emin > emax:
                continue
            for dlo, dhi in product(d_lo, d_hi):
                if dlo > dhi:
                    continue
                for alo, ahi in product(a_lo, a_hi):
                    if alo > ahi:
                        continue
                    rows = subset(trades, dirf, emin, emax, dlo, dhi, alo, ahi)
                    if len(rows) < MIN_N:
                        continue
                    rn, rw, rp = summ(rows)
                    results.append((rp, rn, rw, dirf, emin, emax,
                                    dlo, dhi, alo, ahi))
    print(f"(searched {len(results):,} rule sets with n>={MIN_N})\n")

    print("=" * 64)
    report("ABSOLUTE MAXIMIZER (highest total PnL)",
           max(results, key=lambda x: x[0]))

    zero = [r for r in results if r[2] == r[1]]
    if zero:
        report("ZERO-LOSS (100% win, max PnL among them)",
               max(zero, key=lambda x: x[0]))

    for d in ("up", "dn"):
        sub = [r for r in results if r[3] == d]
        if sub:
            report(f"BEST {d.upper()}-ONLY", max(sub, key=lambda x: x[0]))

    fav = [r for r in results if r[8] >= 0.50 and r[4] <= NEG and r[5] >= POS]
    if fav:
        report("FAVOURITE-STRENGTH only (ASK band, ignore EDGE)",
               max(fav, key=lambda x: x[0]))

    print("=" * 64)
    print("MARGINAL EFFECT of each ceiling alone (vs baseline +%.2f):" % pnl)
    for label, fn in [
        ("EDGE <= 0.13", lambda t: t["edge"] <= 0.13),
        ("DIST <= 1.0", lambda t: t["dist"] <= 1.0),
        ("DIST <= 1.5", lambda t: t["dist"] <= 1.5),
        ("ASK <= 0.60", lambda t: t["ask"] <= 0.60),
        ("ASK <= 0.65", lambda t: t["ask"] <= 0.65),
        ("ASK in [0.50,0.62]", lambda t: 0.50 <= t["ask"] <= 0.62),
    ]:
        rows = [t for t in trades if fn(t)]
        rn, rw, rp = summ(rows)
        wr = f"{rw / rn * 100:.0f}%" if rn else "-"
        print(f"  {label:22} n={rn:>3}  win {wr:>4}  PnL {rp:+.2f}")


if __name__ == "__main__":
    main()
