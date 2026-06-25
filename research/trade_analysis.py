"""Deep P&L / fill analysis of the live bot.

Joins entry features (modeled edge, distance-to-strike, side, market length) to
realized SETTLE P&L, segments by config era (live250 vs live165) and by the
warm-HTTP-client fix, and attributes losses across buckets. Run on the server
(reads journalctl + logs/shadow_taker.jsonl).
"""
import calendar
import json
import re
import subprocess
import statistics as st
from collections import defaultdict

LIVE165 = calendar.timegm((2026, 6, 24, 6, 19, 35, 0, 0, 0))
WARMFIX = calendar.timegm((2026, 6, 24, 21, 26, 35, 0, 0, 0))

TS = re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")


def ep(line):
    m = TS.search(line)
    if not m:
        return None
    return calendar.timegm((int(m.group(1)[0:4]), int(m.group(1)[5:7]),
                            int(m.group(1)[8:10]), int(m.group(1)[11:13]),
                            int(m.group(1)[14:16]), int(m.group(1)[17:19]),
                            0, 0, 0))


def journal():
    out = subprocess.run(["journalctl", "-u", "polybtc.service", "--no-pager"],
                         capture_output=True, text=True).stdout
    return out.splitlines()


def norm(side):
    s = side.upper()
    return "up" if s in ("UP",) else "dn"


def main():
    lines = journal()

    # ---- SETTLE: window -> (ts, outcome, payout, cost, pnl) ----
    settles = []
    rs = re.compile(r"SETTLE (.+?) -> (UP|DOWN) \| payout \$([0-9.]+) "
                    r"cost \$([0-9.]+) pnl \$([+-][0-9.]+)")
    for l in lines:
        m = rs.search(l)
        if not m:
            continue
        win, outc, pay, cost, pnl = m.groups()
        settles.append(dict(ts=ep(l), win=win.strip(), outcome=norm(outc),
                            payout=float(pay), cost=float(cost), pnl=float(pnl)))

    # ---- SNIPE fires: (window, side) -> edge, dsigma ----
    snipes = defaultdict(list)
    for l in lines:
        if " SNIPE " not in l or ": ask" not in l:
            continue
        mw = re.search(r"SNIPE (.+?) (UP|DN): ask ([0-9.]+)", l)
        if not mw:
            continue
        win, side, ask = mw.group(1).strip(), norm(mw.group(2)), float(mw.group(3))
        edge = re.search(r"edge ([0-9.\-]+)", l)
        dsig = re.search(r"d.? ([0-9.\-]+|na)\)", l)
        snipes[(win, side)].append(dict(
            ts=ep(l), ask=ask,
            edge=float(edge.group(1)) if edge else None,
            dsig=(None if (not dsig or dsig.group(1) == "na") else float(dsig.group(1)))))

    def feat(win, held):
        cands = snipes.get((win, held), [])
        if not cands:
            return None, None, None
        c = cands[-1]
        return c["edge"], c["dsig"], c["ask"]

    # attach features + era to each settle
    for s in settles:
        held = s["outcome"] if s["payout"] > 0 else ("dn" if s["outcome"] == "up" else "up")
        s["held"] = held
        s["edge"], s["dsig"], s["ask"] = feat(s["win"], held)
        s["era"] = "live165" if s["cost"] <= 7.0 else "live250"
        s["mlen"] = ("4h" if "AM-" in s["win"] and s["win"].count(":") <= 1 else
                     ("15m" if _is15(s["win"]) else "5m"))

    def block(title, rows):
        if not rows:
            return
        wins = [r for r in rows if r["pnl"] > 0]
        loss = [r for r in rows if r["pnl"] <= 0]
        net = sum(r["pnl"] for r in rows)
        print(f"\n### {title}: n={len(rows)}  net ${net:+.2f}  "
              f"W{len(wins)}/L{len(loss)}  winrate {len(wins)/len(rows)*100:.0f}%")
        if wins:
            print(f"    avg win  ${st.mean(r['pnl'] for r in wins):+.2f}  "
                  f"tot ${sum(r['pnl'] for r in wins):+.2f}")
        if loss:
            print(f"    avg loss ${st.mean(r['pnl'] for r in loss):+.2f}  "
                  f"tot ${sum(r['pnl'] for r in loss):+.2f}")

    print("=" * 72)
    print("SETTLED TRADES — full history")
    block("ALL", settles)
    block("live250 era (cost>$7)", [s for s in settles if s["era"] == "live250"])
    L = [s for s in settles if s["era"] == "live165"]
    block("live165 era (cost<=$7)", L)

    # ---- live165 trade-by-trade ----
    print("\n" + "=" * 72)
    print("live165 TRADES (cost<=$7) — detail")
    print(f"{'window':30} {'held':>4} {'edge':>6} {'dsig':>6} {'cost':>6} {'pnl':>7}")
    for s in sorted(L, key=lambda x: x["ts"] or 0):
        es = "na" if s["edge"] is None else f"{s['edge']:.3f}"
        ds = "na" if s["dsig"] is None else f"{s['dsig']:.2f}"
        print(f"{s['win'][:30]:30} {s['held']:>4} {es:>6} {ds:>6} "
              f"{s['cost']:>6.2f} {s['pnl']:>+7.2f}")

    # ---- attribution on live165 ----
    print("\n" + "=" * 72)
    print("LOSS ATTRIBUTION (live165 era)")

    def bucket(rows, keyfn, order=None):
        d = defaultdict(list)
        for r in rows:
            d[keyfn(r)].append(r)
        keys = order or sorted(d)
        for k in keys:
            if k not in d:
                continue
            block(str(k), d[k])

    print("\n-- by side --")
    bucket(L, lambda r: f"side={r['held']}")
    print("\n-- by edge band --")
    bucket(L, lambda r: ("edge na" if r["edge"] is None else
                         "edge 0.10-0.15" if r["edge"] < 0.15 else
                         "edge 0.15-0.20" if r["edge"] < 0.20 else "edge >=0.20"),
           order=["edge 0.10-0.15", "edge 0.15-0.20", "edge >=0.20", "edge na"])
    print("\n-- by distance-to-strike (sigma) band --")
    bucket(L, lambda r: ("dsig na" if r["dsig"] is None else
                         "dsig <0.5" if r["dsig"] < 0.5 else
                         "dsig 0.5-1.0" if r["dsig"] < 1.0 else "dsig >=1.0"),
           order=["dsig <0.5", "dsig 0.5-1.0", "dsig >=1.0", "dsig na"])

    # ---- shadow: fill rate + latency before/after warm fix ----
    print("\n" + "=" * 72)
    print("FILL RATE & LATENCY (shadow_taker.jsonl)")
    recs = []
    try:
        with open("logs/shadow_taker.jsonl") as f:
            for line in f:
                try:
                    recs.append(json.loads(line))
                except Exception:
                    pass
    except FileNotFoundError:
        print("  (no shadow log)")
        recs = []

    def filled(r):
        return r.get("avg_fill_px") is not None or (
            r.get("capture_frac") is not None and "reject" not in str(r.get("status", "")))

    for label, lo, hi in [("ALL TIME", 0, 1e18),
                          ("live165 (pre warm-fix)", LIVE165, WARMFIX),
                          ("post warm-fix", WARMFIX, 1e18)]:
        seg = [r for r in recs if lo <= r.get("ts", 0) < hi]
        if not seg:
            continue
        fl = [r for r in seg if filled(r)]
        posts = [r["post_ms"] for r in seg if isinstance(r.get("post_ms"), (int, float))]
        rej = [r for r in seg if "reject" in str(r.get("status", ""))]
        reqexc = [r for r in seg if "Request exception" in str(r.get("status", ""))]
        print(f"\n### {label}: attempts={len(seg)}  filled={len(fl)} "
              f"({len(fl)/len(seg)*100:.0f}%)  rejected={len(rej)}  "
              f"req-exc={len(reqexc)}")
        if posts:
            posts.sort()
            print(f"    post_ms: median {st.median(posts):.0f}  "
                  f"p90 {posts[int(len(posts)*0.9)]:.0f}  max {max(posts):.0f}")
        caps = [r["capture_frac"] for r in fl
                if isinstance(r.get("capture_frac"), (int, float))]
        if caps:
            print(f"    capture_frac (filled): median {st.median(caps):.2f}")


def _is15(win):
    m = re.findall(r"(\d+):(\d+)([AP]M)", win)
    if len(m) != 2:
        return False
    def mins(h, mm, ap):
        h = int(h) % 12
        if ap == "PM":
            h += 12
        return h * 60 + int(mm)
    return mins(*m[1]) - mins(*m[0]) in (15, -705, 735)


if __name__ == "__main__":
    main()
