"""Sizing/risk backtester for the 1000-cap config.

WHAT THIS DOES (and its honest limits)
--------------------------------------
The entry *decisions* a paper session makes (which market, which side, at what
ask, with what model edge, and the eventual UP/DOWN resolution) are essentially
independent of how much capital we deploy -- they're driven by the Binance-lag
signal, not the bankroll. What DOES depend on capital is *position sizing*.

So we take the realized stream of settled snipe fills from real paper sessions
(each carries: decision ts, market, side, entry price, model edge, realized
cost, realized pnl, win/loss) and *re-size* every fill under alternative
policies, then rebuild the equity curve. Because per-share pnl is linear in
shares, scaling a fill's stake scales its pnl exactly:
        pnl(stake) = (pnl_realized / cost_realized) * stake

FAITHFULNESS RULE: we only ever scale a fill's stake DOWN from what really
filled (stake <= cost_realized). Buying *fewer* shares is always executable;
assuming we could have bought *more* than the book actually showed is not. So
this optimiser explores the "size down / re-weight / cap" space -- exactly the
lever that turned the 1000-cap negative -- and cannot fabricate upside by
inventing liquidity that wasn't there.

Frictions (taker fees, FAK race losses, 30% partial-fill capture haircut) are
already baked into the realized pnl and are preserved.
"""
import glob
import re
import sys
from collections import defaultdict
from datetime import datetime

SNIPE_RE = re.compile(
    r"^(?P<ts>[\d-]+ [\d:,]+) \S+\s+INFO\s+SNIPE (?P<title>.+?) (?P<side>UP|DN): "
    r"ask (?P<ask>[\d.]+)(?: \(limit [\d.]+\))? \+ fee (?P<fee>[\d.]+) vs robust "
    r"(?P<robust>[\d.]+) \(blend (?P<blend>[\d.]+), edge (?P<edge>[\d.]+), \$(?P<usd>[\d.]+)\)"
)
FILL_RE = re.compile(
    r"^(?P<ts>[\d-]+ [\d:,]+) \S+\s+INFO\s+FILL (?P<side>UP|DN)\s+(?P<leg>\w+)\s+"
    r"(?P<title>.+?)\s+(?P<sh>[\d.]+) sh @ (?P<px>[\d.]+) \(\$(?P<cost>[\d.]+)"
    r"(?: \+fee (?P<fee>[\d.]+))?\)"
)
SETTLE_RE = re.compile(
    r"^(?P<ts>[\d-]+ [\d:,]+) \S+\s+INFO\s+SETTLE (?P<title>.+?) -> (?P<out>UP|DOWN) "
    r"\| payout \$(?P<pay>[\d.]+) cost \$(?P<cost>-?[\d.]+) pnl \$(?P<pnl>[+-][\d.]+)"
)


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


def load_events(path):
    """Return list of settled SNIPE fill events for one session, time-ordered.

    Each event: decision ts, settle ts, market title/kind, side, entry price,
    model edge, realized cost, realized pnl-per-dollar (ret), realized pnl, won.
    Scalp fills are excluded (different leg/thesis); we optimise the snipe book.
    """
    snipes, fills, settles, settle_ts = [], [], {}, {}
    with open(path) as f:
        for line in f:
            if (m := SNIPE_RE.match(line)):
                d = m.groupdict()
                d["ts"] = parse_ts(d["ts"])
                snipes.append(d)
            elif (m := FILL_RE.match(line)):
                d = m.groupdict()
                if d["leg"] != "snipe":
                    continue
                d["ts"] = parse_ts(d["ts"])
                fills.append(d)
            elif (m := SETTLE_RE.match(line)):
                d = m.groupdict()
                settles[d["title"]] = d
                settle_ts[d["title"]] = parse_ts(d["ts"])

    events = []
    for fl in fills:
        s = settles.get(fl["title"])
        if not s:
            continue
        side = "up" if fl["side"] == "UP" else "dn"
        won = (s["out"] == "UP") == (side == "up")
        sh, px = float(fl["sh"]), float(fl["px"])
        fee = float(fl["fee"] or 0.0)
        cost = sh * px
        if cost <= 0:
            continue
        pnl = (sh if won else 0.0) - cost - fee
        # nearest preceding snipe-intent on same title+side -> model edge
        edge = None
        best = None
        for sn in snipes:
            if sn["title"] == fl["title"] and sn["side"] == fl["side"] \
                    and sn["ts"] <= fl["ts"] + 1.0:
                if best is None or sn["ts"] > best:
                    best, edge = sn["ts"], float(sn["edge"])
        events.append(dict(
            dts=fl["ts"], sts=settle_ts[fl["title"]], title=fl["title"],
            kind=kind_of(fl["title"]), side=side, px=px, cost=cost,
            pnl=pnl, ret=pnl / cost, won=won, edge=edge,
        ))
    events.sort(key=lambda e: e["dts"])
    return events


# --------------------------------------------------------------------------
# sizing policies: given (equity, event) -> desired stake in $ (clamped to the
# realized cost so we never invent liquidity). Caps applied by the engine.
# --------------------------------------------------------------------------

def policy_baseline():
    """Realized 1000-cap behaviour: take the full realized stake."""
    def f(eq, e, st):
        return e["cost"]
    return ("baseline (as-run 1000-cap)", f, dict())


def policy_flat(cap):
    def f(eq, e, st):
        return min(e["cost"], cap)
    return (f"flat cap ${cap}/trade", f, dict())


def policy_flat_skip5m_bigedge(cap, edge_skip):
    def f(eq, e, st):
        if e["edge"] is not None and e["edge"] >= edge_skip:
            return 0.0
        return min(e["cost"], cap)
    label = (f"skip edge>={edge_skip} (full size)" if cap >= 100
             else f"flat ${cap} + skip edge>={edge_skip}")
    return (label, f, dict())


def policy_flat_pct(pct, hard_cap):
    """Flat fraction of running equity per trade (compounding), hard-capped."""
    def f(eq, e, st):
        return min(e["cost"], hard_cap, pct * eq)
    return (f"flat {pct*100:.1f}% equity (<=${hard_cap})", f, dict())


def policy_skip_5m(cap):
    """Flat cap, but skip 5m markets (the consistently negative kind)."""
    def f(eq, e, st):
        if e["kind"] == "5m":
            return 0.0
        return min(e["cost"], cap)
    return (f"flat ${cap}, no 5m", f, dict())


def policy_combo(cap, edge_skip):
    """Flat cap + skip winner's-curse edges + downweight 5m to 1/2."""
    def f(eq, e, st):
        if e["edge"] is not None and e["edge"] >= edge_skip:
            return 0.0
        c = cap * (0.5 if e["kind"] == "5m" else 1.0)
        return min(e["cost"], c)
    return (f"combo ${cap}/half-5m/skip>={edge_skip}", f, dict())


# --------------------------------------------------------------------------
# engine: replay one session under a policy with exposure cap + drawdown
# --------------------------------------------------------------------------

def run(events, policy_fn, start_cash, max_exposure, per_market_cap=1e9):
    import heapq
    settled_pnl = 0.0
    pending = []           # (settle_ts, pnl) heap
    exposure = 0.0
    mkt_open = defaultdict(float)
    eq_curve = [start_cash]
    trade_rets = []
    deployed = 0.0
    wins = trades = 0

    def realize(until_ts):
        nonlocal settled_pnl, exposure
        while pending and pending[0][0] <= until_ts:
            _, pnl, stake, mslug = heapq.heappop(pending)
            settled_pnl += pnl
            exposure -= stake
            mkt_open[mslug] -= stake
            eq_curve.append(start_cash + settled_pnl)

    for e in events:
        realize(e["dts"])
        equity = start_cash + settled_pnl
        want = policy_fn(equity, e, exposure)
        want = min(want, e["cost"])                       # faithful: down only
        want = min(want, max(0.0, max_exposure - exposure))
        want = min(want, max(0.0, per_market_cap - mkt_open[e["title"]]))
        if want <= 0.5:
            continue
        pnl = e["ret"] * want
        heapq.heappush(pending, (e["sts"], pnl, want, e["title"]))
        exposure += want
        mkt_open[e["title"]] += want
        deployed += want
        trades += 1
        wins += e["won"]
        trade_rets.append(e["ret"])
    realize(float("inf"))

    final = start_cash + settled_pnl
    peak = start_cash
    maxdd = 0.0
    for v in eq_curve:
        peak = max(peak, v)
        maxdd = max(maxdd, peak - v)
    import statistics as st
    sharpe = (st.mean(trade_rets) / st.pstdev(trade_rets)
              if len(trade_rets) > 1 and st.pstdev(trade_rets) > 0 else 0.0)
    return dict(
        pnl=settled_pnl, final=final, deployed=deployed, trades=trades,
        winrate=wins / trades if trades else 0.0, maxdd=maxdd,
        roi_dep=settled_pnl / deployed if deployed else 0.0,
        roi_cap=settled_pnl / start_cash, sharpe=sharpe,
    )


def bucket_table(title, events, keyfn):
    groups = defaultdict(list)
    for e in events:
        groups[keyfn(e)].append(e)
    print(f"--- {title} ---")
    print(f"{'bucket':>12} {'n':>4} {'win%':>6} {'cost$':>9} {'pnl$':>9} {'ret/$':>7}")
    for k in sorted(groups, key=str):
        g = groups[k]
        c = sum(e["cost"] for e in g)
        p = sum(e["pnl"] for e in g)
        w = sum(e["won"] for e in g) / len(g)
        print(f"{str(k):>12} {len(g):>4} {w:>6.0%} {c:>9.0f} {p:>+9.0f} "
              f"{p/c if c else 0:>+7.1%}")
    print()


def main():
    paths = sys.argv[1:] or [
        "logs/session_1781925965.log",   # Jun 19 ~23:25 windows
        "logs/session_1781839742.log",   # Jun 18 ~23:25 windows
        "logs/session_1781795209.log",   # Jun 18 ~11:05 windows
        "logs/session_1781777645.log",   # Jun 18 ~06:10 windows
        "logs/session_1781683845.log",   # Jun 17 (older code, out-of-sample)
    ]
    sessions = [(p, load_events(p)) for p in paths]
    sessions = [(p, ev) for p, ev in sessions if ev]
    allev = [e for _, ev in sessions for e in ev]
    print("loaded sessions:")
    for p, ev in sessions:
        print(f"  {p.split('/')[-1]:32} {len(ev):3} snipe fills")
    print(f"  TOTAL {len(allev)} settled snipe fills\n")

    print("=" * 60)
    print("POOLED DIAGNOSTICS (realized, all sessions) — where the edge is")
    print("=" * 60)
    bucket_table("by market kind", allev, lambda e: e["kind"])
    bucket_table("by entry price", allev,
                 lambda e: f"{int(e['px']*10)/10:.1f}-{int(e['px']*10)/10+.1:.1f}")
    bucket_table("by model edge", allev, lambda e: "?" if e["edge"] is None
                 else f"{min(0.25,int(e['edge']*20)/20):.2f}+")
    bucket_table("by realized stake $", allev, lambda e: "<10" if e["cost"] < 10
                 else "10-30" if e["cost"] < 30 else "30-60" if e["cost"] < 60
                 else "60+")

    START = 1000.0
    policies = [
        policy_baseline(),
        policy_flat(40), policy_flat(30), policy_flat(25), policy_flat(20),
        policy_flat_skip5m_bigedge(100, 0.20),   # isolate the edge>=0.20 cut
        policy_flat_skip5m_bigedge(40, 0.20),
        policy_flat_skip5m_bigedge(30, 0.20),
        policy_flat_skip5m_bigedge(25, 0.20),
        policy_flat_skip5m_bigedge(20, 0.20),
    ]
    EXP = {"baseline (as-run 1000-cap)": 500}

    import statistics as st
    hdr = (f"{'policy':38} {'pnl$':>7} {'ROI/$':>7} {'maxDD':>6} "
           f"{'win%':>5} {'trd':>4} | {'per-session pnl ($)':>34} {'worst':>7} {'consist':>8}")
    print("=" * len(hdr))
    print("POLICY SWEEP — each session starts $1000; engine scales stakes DOWN only")
    print("=" * len(hdr))
    print(hdr)
    print("-" * len(hdr))
    ranked = []
    for name, fn, opts in policies:
        exp = EXP.get(name, 150)
        per = []
        tot = defaultdict(float)
        dds = []
        for p, ev in sessions:
            r = run(ev, fn, START, max_exposure=exp, per_market_cap=exp)
            per.append(r["pnl"])
            tot["pnl"] += r["pnl"]
            tot["deployed"] += r["deployed"]
            tot["trades"] += r["trades"]
            tot["wins"] += r["winrate"] * r["trades"]
            dds.append(r["maxdd"])
        roi_dep = tot["pnl"] / tot["deployed"] if tot["deployed"] else 0.0
        win = tot["wins"] / tot["trades"] if tot["trades"] else 0.0
        worst = min(per)
        # consistency: mean session pnl / stdev (higher = steadier, less variance)
        consist = (st.mean(per) / st.pstdev(per)) if len(per) > 1 and st.pstdev(per) else 0.0
        psv = " ".join(f"{x:>+6.0f}" for x in per)
        print(f"{name:38} {tot['pnl']:>+7.0f} {roi_dep:>+7.1%} {max(dds):>6.0f} "
              f"{win:>5.0%} {int(tot['trades']):>4} | {psv:>34} {worst:>+7.0f} {consist:>8.2f}")
        ranked.append((name, tot["pnl"], roi_dep, worst, consist))
    print("-" * len(hdr))
    print("per-session order:", ", ".join(p.split('_')[-1].replace('.log','')
          for p, _ in sessions))
    print("\nROI/$ = pnl per dollar deployed; consist = mean(session pnl)/stdev "
          "(higher=steadier); worst = worst single-session pnl (tail risk)")


if __name__ == "__main__":
    main()
