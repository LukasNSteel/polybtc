"""Deep single-session forensics for logs/session_1781683845.log.

Joins every taker fill to its settlement, breaks PnL down by leg/kind/side/
price/edge/hour, reconstructs the BTC price regime and equity curve from the
strategy heartbeat, and reports race-loss / adverse-selection / markout stats
with Wilson win-rate CIs and bootstrap EV/$ CIs.

Usage: python research/analyze_session_1781683845.py [logfile]
"""

import math
import random
import re
import sys
from collections import defaultdict
from datetime import datetime

PATH = sys.argv[1] if len(sys.argv) > 1 else "logs/session_1781683845.log"

SNIPE_RE = re.compile(
    r"^(?P<ts>[\d-]+ [\d:,]+) \S+\s+INFO\s+SNIPE (?P<title>.+?) (?P<side>UP|DN): "
    r"ask (?P<ask>[\d.]+) \+ fee (?P<fee>[\d.]+) vs robust (?P<robust>[\d.]+) "
    r"\(blend (?P<blend>[\d.]+), edge (?P<edge>[\d.]+), \$(?P<usd>[\d.]+)\)"
)
SCALP_RE = re.compile(
    r"^(?P<ts>[\d-]+ [\d:,]+) \S+\s+INFO\s+SCALP (?P<title>.+?) (?P<side>UP|DN) @ "
    r"(?P<px>[\d.]+) \(fair (?P<fair>[\d.]+), (?P<tau>\d+)s left\)"
)
FILL_RE = re.compile(
    r"^(?P<ts>[\d-]+ [\d:,]+) \S+\s+INFO\s+FILL (?P<side>UP|DN)\s+(?P<leg>\w+)\s+"
    r"(?P<title>.+?)\s+(?P<sh>[\d.]+) sh @ (?P<px>[\d.]+) "
    r"\(\$(?P<cost>[\d.]+)(?: \+fee (?P<fee>[\d.]+))?\)"
)
SETTLE_RE = re.compile(
    r"^(?P<ts>[\d-]+ [\d:,]+) \S+\s+INFO\s+SETTLE (?P<title>.+?) -> (?P<out>UP|DOWN) "
    r"\| payout \$(?P<pay>[\d.]+) cost \$(?P<cost>-?[\d.]+) pnl \$(?P<pnl>[+-][\d.]+)"
)
REJECT_RE = re.compile(
    r"paper FAK rejected: (?P<title>.+?) (?P<side>UP|DN), ask (?P<ask>[\d.]+) vs "
    r"limit (?P<limit>[\d.]+) after \d+ms hold \(fair drift (?P<drift>[+-][\d.]+)"
)
ADVERSE_RE = re.compile(r"paper ADVERSE FILL.*fair drifted (?P<drift>[+-][\d.]+)")
HB_RE = re.compile(
    r"^(?P<ts>[\d-]+ [\d:,]+) strategy\s+INFO\s+spot (?P<spot>[\d.]+) "
    r"\(perp basis (?P<perp>[+-]?[\d.]+), cb basis (?P<cb>[+-]?[\d.]+)\) \| "
    r"vol\(1m\) (?P<vol>[\d.]+)% .*?\| cash \$(?P<cash>[\d.]+) \| "
    r"equity \$(?P<equity>[\d.]+) \| exposure \$(?P<exp>[\d.]+)"
)
JUMP_RE = re.compile(r"JUMP GUARD: (?P<bps>[\d.]+) bps")
TIER_RE = re.compile(r"FAK STRESS tier (?P<from>\d)\D+(?P<to>\d)")


def parse_ts(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S,%f")


def kind_of(title):
    m = re.search(r"(\d+):(\d+)(AM|PM)-(\d+):(\d+)(AM|PM)", title)
    if not m:
        return "1h" if re.search(r"\d+(AM|PM) ET", title) else "?"
    h1, m1, ap1, h2, m2, ap2 = m.groups()
    t1 = (int(h1) % 12 + (12 if ap1 == "PM" else 0)) * 60 + int(m1)
    t2 = (int(h2) % 12 + (12 if ap2 == "PM" else 0)) * 60 + int(m2)
    d = (t2 - t1) % (24 * 60)
    return {5: "5m", 15: "15m", 60: "1h", 240: "4h"}.get(d, f"{d}m")


def wilson(k, n, z=1.96):
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return ((c - h) / d, (c + h) / d)


def boot_ci(vals, costs, iters=5000):
    """Bootstrap CI for sum(pnl)/sum(cost) = EV per $."""
    if not vals or sum(costs) == 0:
        return (0.0, 0.0)
    n = len(vals)
    idx = range(n)
    out = []
    for _ in range(iters):
        s = [random.randrange(n) for _ in idx]
        c = sum(costs[i] for i in s)
        if c:
            out.append(sum(vals[i] for i in s) / c)
    out.sort()
    return (out[int(0.025 * len(out))], out[int(0.975 * len(out))])


snipes, scalps, fills = [], [], []
settles = {}
rejects, advers, jumps, tiers = [], [], [], []
hbs = []

with open(PATH) as f:
    for line in f:
        for rx, bucket in ((SNIPE_RE, snipes), (SCALP_RE, scalps)):
            m = rx.match(line)
            if m:
                bucket.append(m.groupdict())
                break
        else:
            m = FILL_RE.match(line)
            if m:
                fills.append(m.groupdict())
                continue
            m = SETTLE_RE.match(line)
            if m:
                settles[m.group("title")] = m.groupdict()
                continue
            m = HB_RE.match(line)
            if m:
                hbs.append(m.groupdict())
                continue
            m = REJECT_RE.search(line)
            if m:
                rejects.append(m.groupdict())
                continue
            m = ADVERSE_RE.search(line)
            if m:
                advers.append(float(m.group("drift")))
                continue
            m = JUMP_RE.search(line)
            if m:
                jumps.append(float(m.group("bps")))
                continue
            m = TIER_RE.search(line)
            if m:
                tiers.append((m.group("from"), m.group("to")))

# join fills -> settlement
rows = []
for fl in fills:
    s = settles.get(fl["title"])
    if not s:
        continue
    side = "up" if fl["side"] == "UP" else "dn"
    won = (s["out"] == "UP") == (side == "up")
    sh, px, fee = float(fl["sh"]), float(fl["px"]), float(fl["fee"] or 0)
    pnl = (sh if won else 0.0) - sh * px - fee
    ft = parse_ts(fl["ts"])
    edge = robust = None
    best = None
    pool = snipes if fl["leg"] == "snipe" else scalps
    for sn in pool:
        if sn["title"] == fl["title"] and sn.get("side") == fl["side"]:
            st = parse_ts(sn["ts"])
            if st <= ft and (best is None or st > best):
                best = st
                if fl["leg"] == "snipe":
                    edge, robust = float(sn["edge"]), float(sn["robust"])
    rows.append(dict(title=fl["title"], kind=kind_of(fl["title"]), leg=fl["leg"],
                     side=side, px=px, sh=sh, cost=sh * px, fee=fee, won=won,
                     pnl=pnl, edge=edge, robust=robust, ts=ft, hour=ft.hour))

t0, t1 = parse_ts(fills[0]["ts"]), parse_ts(fills[-1]["ts"])
print("=" * 72)
print(f"SESSION {PATH}")
print(f"span  {t0:%Y-%m-%d %H:%M} -> {t1:%Y-%m-%d %H:%M}  ({(t1-t0).total_seconds()/3600:.1f}h)")
print("=" * 72)

total = sum(r["pnl"] for r in rows)
cost = sum(r["cost"] for r in rows)
fee = sum(r["fee"] for r in rows)
print(f"\n{len(fills)} fills, {len(rows)} joined to settlement")
print(f"settled PnL ${total:+.2f} on ${cost:.2f} taker cost ({total/cost:+.1%}/$), fees ${fee:.2f}")


def table(name, keyfn, rows, sortnum=False):
    g = defaultdict(list)
    for r in rows:
        g[keyfn(r)].append(r)
    print(f"\n--- by {name} ---")
    print(f"{'group':>12} {'n':>4} {'win%':>5} {'wilsonCI':>13} "
          f"{'cost$':>8} {'pnl$':>9} {'EV/$':>7} {'EV/$ 95% CI':>16}")
    def numkey(k):
        m = re.search(r"-?\d+\.?\d*", str(k))
        return float(m.group()) if m else 0.0
    keys = sorted(g, key=(numkey if sortnum else str))
    for k in keys:
        rr = g[k]
        n = len(rr)
        w = sum(r["won"] for r in rr)
        c = sum(r["cost"] for r in rr)
        p = sum(r["pnl"] for r in rr)
        lo, hi = wilson(w, n)
        elo, ehi = boot_ci([r["pnl"] for r in rr], [r["cost"] for r in rr])
        print(f"{str(k):>12} {n:>4} {w/n:>5.0%} [{lo:>4.0%},{hi:>4.0%}] "
              f"{c:>8.0f} {p:>+9.2f} {p/c if c else 0:>+7.1%} [{elo:>+6.1%},{ehi:>+6.1%}]")


sn = [r for r in rows if r["leg"] == "snipe"]
sc = [r for r in rows if r["leg"] == "scalp"]
table("leg", lambda r: r["leg"], rows)
table("kind (snipe)", lambda r: r["kind"], sn)
table("side (snipe)", lambda r: r["side"], sn)
table("ask bucket (snipe)", lambda r: f"{int(r['px']*10)/10:.1f}-{int(r['px']*10)/10+0.1:.1f}", sn, True)
table("ask 5c (snipe)", lambda r: f"{int(r['px']*20)/20:.2f}", sn, True)
table("kind x side (snipe)", lambda r: f"{r['kind']}-{r['side']}", sn)
table("day (snipe)", lambda r: f"{r['ts']:%m-%d}", sn)
table("edge bucket (snipe)", lambda r: "?" if r["edge"] is None else f"{int(r['edge']*20)/20:.2f}+", sn, True)
table("size$ (snipe)", lambda r: "a<25" if r["cost"] < 25 else "b25-50" if r["cost"] < 50 else "c50-75" if r["cost"] < 75 else "d75+", sn)

# hour-of-day (UTC)
table("hourUTC (snipe)", lambda r: f"{r['hour']:02d}", sn, True)

# ---- regime reconstruction from heartbeat ----
print("\n" + "=" * 72)
print("PRICE REGIME (from strategy heartbeat)")
print("=" * 72)
spots = [(parse_ts(h["ts"]), float(h["spot"])) for h in hbs]
vols = [float(h["vol"]) for h in hbs]
perps = [float(h["perp"]) for h in hbs]
cbs = [float(h["cb"]) for h in hbs]
eqs = [(parse_ts(h["ts"]), float(h["equity"]), float(h["cash"]), float(h["exp"])) for h in hbs]
p_open, p_close = spots[0][1], spots[-1][1]
p_hi, p_lo = max(s for _, s in spots), min(s for _, s in spots)
print(f"spot   open {p_open:.0f}  close {p_close:.0f}  "
      f"net {p_close-p_open:+.0f} ({(p_close/p_open-1)*100:+.2f}%)  "
      f"range [{p_lo:.0f}, {p_hi:.0f}] = {(p_hi-p_lo)/p_open*1e4:.0f} bps")
# realized vol per 10s step, annualize-ish: report as 1m-equiv from heartbeat
rets = [math.log(spots[i][1]/spots[i-1][1]) for i in range(1, len(spots))]
import statistics as st
rv = st.pstdev(rets)
print(f"10s log-ret stdev {rv*1e4:.1f} bps  | mean {st.mean(rets)*1e4:+.2f} bps/10s")
print(f"vol(1m) heartbeat: mean {st.mean(vols):.3f}%  median {st.median(vols):.3f}%  "
      f"p95 {sorted(vols)[int(0.95*len(vols))]:.3f}%  max {max(vols):.3f}%")
print(f"perp basis: mean {st.mean(perps):+.1f}  range [{min(perps):+.1f},{max(perps):+.1f}]")
print(f"cb basis:   mean {st.mean(cbs):+.1f}  range [{min(cbs):+.1f},{max(cbs):+.1f}]")
print(f"jump-guard trips: {len(jumps)} (mean {st.mean(jumps):.1f} bps)" if jumps else "jump-guard trips: 0")

# settlement outcome balance (regime up/down split)
outs = [s["out"] for s in settles.values()]
nu = outs.count("UP")
print(f"\nsettlements observed: {len(outs)}  UP {nu} ({nu/len(outs):.0%})  DOWN {len(outs)-nu}")

# ---- equity / drawdown ----
print("\n" + "=" * 72)
print("EQUITY CURVE & DRAWDOWN (mark-to-market equity)")
print("=" * 72)
peak = -1e9
maxdd = 0.0
peak_eq = eqs[0][1]
for _, eq, _, _ in eqs:
    peak = max(peak, eq)
    maxdd = min(maxdd, eq - peak)
final_eq = eqs[-1][1]
print(f"equity start {eqs[0][1]:.0f}  peak {peak:.0f}  end {final_eq:.0f}")
print(f"max equity drawdown {maxdd:+.0f} ({maxdd/peak*100:.1f}% from peak)")
print(f"realized cash end {eqs[-1][2]:.0f}; open exposure {eqs[-1][3]:.0f}")

# ---- FAK / race / adverse ----
print("\n" + "=" * 72)
print("EXECUTION QUALITY (FAK race, adverse selection)")
print("=" * 72)
n_snipe_intent = len(snipes)
n_filled = len([r for r in rows if r["leg"] == "snipe"])
print(f"snipe intents logged: {n_snipe_intent}  scalp intents: {len(scalps)}")
print(f"FAK rejects logged: {len(rejects)}  (richen/gone races lost)")
if rejects:
    rd = [float(x["drift"]) for x in rejects]
    print(f"   reject fair-drift mean {st.mean(rd):+.3f}  median {st.median(rd):+.3f}")
    # rejected by side
    rs = defaultdict(int)
    for x in rejects:
        rs[x["side"]] += 1
    print(f"   rejects by side: {dict(rs)}")
print(f"adverse fills: {len(advers)}  mean drift {st.mean(advers):+.3f}" if advers else "adverse fills: 0")
print(f"FAK stress-tier transitions: {tiers}")

# ---- worst / best ----
sn.sort(key=lambda r: r["pnl"])
print("\n--- 8 worst snipe fills ---")
for r in sn[:8]:
    print(f"  {r['pnl']:+8.2f}  {r['kind']:>3} {r['side']} @{r['px']:.2f} x{r['sh']:.0f} "
          f"edge={r['edge']} robust={r['robust']}  {r['title']}")
print("--- 8 best snipe fills ---")
for r in sn[-8:]:
    print(f"  {r['pnl']:+8.2f}  {r['kind']:>3} {r['side']} @{r['px']:.2f} x{r['sh']:.0f} "
          f"edge={r['edge']} robust={r['robust']}  {r['title']}")

# overall EV/$ CI for snipe
elo, ehi = boot_ci([r["pnl"] for r in sn], [r["cost"] for r in sn])
w = sum(r["won"] for r in sn)
lo, hi = wilson(w, len(sn))
print(f"\nSNIPE overall: n={len(sn)} win {w/len(sn):.0%} [{lo:.0%},{hi:.0%}]  "
      f"EV/$ {sum(r['pnl'] for r in sn)/sum(r['cost'] for r in sn):+.1%} [{elo:+.1%},{ehi:+.1%}]")

# breakeven win-rate check (favorite-side): need win > avg_px + avg_fee_frac
avg_px = sum(r["cost"] for r in sn) / sum(r["sh"] for r in sn)
avg_feefrac = sum(r["fee"] for r in sn) / sum(r["cost"] for r in sn)
print(f"avg fill px {avg_px:.3f}, fee {avg_feefrac:.1%} of notional -> "
      f"breakeven win-rate ~{avg_px*(1+avg_feefrac):.1%}; realized {w/len(sn):.1%}")

# does size track edge / realized EV? (it shouldn't be flat or negative)
import statistics as st2
big = [r for r in sn if r["cost"] >= 60]
small = [r for r in sn if r["cost"] < 60]
def evd(g):
    c = sum(r["cost"] for r in g); return sum(r["pnl"] for r in g)/c if c else 0
print(f"\nsize check: small(<$60) n={len(small)} EV/$ {evd(small):+.1%} | "
      f"big(>=$60) n={len(big)} EV/$ {evd(big):+.1%}")
edge_big = st2.mean([r["edge"] for r in big if r["edge"]])
edge_small = st2.mean([r["edge"] for r in small if r["edge"]])
print(f"  mean edge: small {edge_small:.3f}  big {edge_big:.3f}  "
      f"(if ~equal, larger size carries no extra edge)")

# reconcile to session summary
print(f"\nRECONCILE: settled-PnL ${total:+.2f} - open cost basis ~$152 "
      f"= realized cash ~${total-152:+.0f} (summary: +418.68)")
