"""Test 6: market-maker leg simulation from taker prints.

At each snapshot gridpoint we place passive bids on BOTH sides at
(side price - delta), keep them until the next gridpoint (repricing), and
detect fills from actual taker SELL prints:

  - conservative fill: a print at price strictly BELOW our bid (level swept)
  - optimistic fill:   a print at price <= our bid (we were in the queue)

PnL per fill = outcome - bid (maker pays no fee; rebates ignored = extra margin).
This brackets the true MM economics without book data.
"""

import numpy as np
import pandas as pd

D = "research/data"
rng = np.random.default_rng(3)

tt = pd.read_parquet(f"{D}/taker_trades.parquet")
sn = pd.read_parquet(f"{D}/snapshots.parquet")

# taker SELL of side s at price pi == print available to fill maker bids on s at >= pi.
# in normalized terms: a taker "bought" side x at q means they SOLD side (1-x) at 1-q.
tt["sold_up"] = ~tt.bought_up
tt["sell_px"] = 1 - tt.q          # price at which the sold side traded (maker side price)

GRID_STEP = {"5m": 30, "15m": 60, "1h": 300, "4h": 1200}
STOP_SEC = {"5m": 25, "15m": 25, "1h": 60, "4h": 120}
DELTAS = [0.02, 0.03, 0.04, 0.06, 0.08]

results = []
for kind, g in tt.groupby("kind"):
    step = GRID_STEP[kind]
    stop = STOP_SEC[kind]
    snk = sn[sn.kind == kind]
    sells = {s: x for s, x in g.groupby("slug")}
    for slug, ss in snk.groupby("slug"):
        prints = sells.get(slug)
        if prints is None:
            continue
        pts = prints.sort_values("ts")
        ts_arr = pts.ts.values
        y_up = int(ss.y_up.iloc[0])
        for _, row in ss.iterrows():
            tau = row.tau
            if tau <= stop:
                continue
            t0 = row.tau  # time remaining at quote placement
            # quote lifetime: until next reprice or stop
            life = min(step, t0 - stop)
            if life <= 0:
                continue
            # absolute window: prints with tau in (t0 - life, t0]
            for side_up, p_side in [(True, row.p_up), (False, 1 - row.p_up)]:
                y_side = y_up if side_up else 1 - y_up
                for d in DELTAS:
                    bid = round(p_side - d, 3)
                    if bid < 0.10 or bid > 0.85:
                        continue
                    m = pts[(pts.tau < t0) & (pts.tau >= t0 - life) &
                            (pts.sold_up == side_up)]
                    if not len(m):
                        continue
                    swept = m[m.sell_px < bid - 1e-9]
                    touched = m[m.sell_px <= bid + 1e-9]
                    results.append({
                        "kind": kind, "slug": slug, "tau": tau, "delta": d,
                        "fill_cons": int(len(swept) > 0),
                        "fill_opt": int(len(touched) > 0),
                        "pnl_cons": (y_side - bid) if len(swept) else 0.0,
                        "pnl_opt": (y_side - bid) if len(touched) else 0.0,
                    })

r = pd.DataFrame(results)
print(f"quote-events: {len(r)}")

print("\n=== MM economics by kind x delta (per quote placed; cents/share when filled) ===")
print("cons = fill only when price swept through; opt = touched our level")
for kind in ["5m", "15m", "1h", "4h"]:
    rk = r[r.kind == kind]
    if not len(rk):
        continue
    print(f"\n[{kind}]")
    for d in DELTAS:
        rd = rk[rk.delta == d]
        if len(rd) < 200:
            continue
        fc = rd.fill_cons.mean()
        fo = rd.fill_opt.mean()
        pc = rd[rd.fill_cons == 1].pnl_cons
        po = rd[rd.fill_opt == 1].pnl_opt
        # cluster bootstrap over markets for conservative per-fill pnl
        slugs = rd[rd.fill_cons == 1].slug.unique()
        gs = {s: x for s, x in rd[rd.fill_cons == 1].groupby("slug")}
        bs = []
        for _ in range(200):
            pick = rng.choice(slugs, size=len(slugs), replace=True) if len(slugs) else []
            if not len(pick):
                break
            bs.append(pd.concat([gs[s] for s in pick]).pnl_cons.mean())
        ci = (np.percentile(bs, [2.5, 97.5]) if bs else [np.nan, np.nan])
        print(f"  delta={100*d:.0f}c: fill% cons={100*fc:5.1f} opt={100*fo:5.1f} | "
              f"pnl/fill cons={100*pc.mean() if len(pc) else float('nan'):+6.2f}c "
              f"[{100*ci[0]:+.2f},{100*ci[1]:+.2f}] "
              f"opt={100*po.mean() if len(po) else float('nan'):+6.2f}c | "
              f"EV/quote cons={100*rd.pnl_cons.mean():+5.3f}c")

print("\n=== MM by tau bucket (delta=4c, conservative) ===")
for kind in ["5m", "15m", "1h"]:
    rk = r[(r.kind == kind) & (r.delta == 0.04)]
    if not len(rk):
        continue
    qs = [0, 0.25, 0.5, 0.75, 1.0]
    edges = rk.tau.quantile(qs).values
    print(f"[{kind}]")
    for i in range(4):
        b = rk[(rk.tau >= edges[i]) & (rk.tau <= edges[i + 1])]
        f = b[b.fill_cons == 1]
        if len(f) < 30:
            continue
        print(f"  tau {edges[i]:6.0f}-{edges[i+1]:6.0f}: fills={len(f):5d} "
              f"fill%={100*b.fill_cons.mean():5.1f} pnl/fill={100*f.pnl_cons.mean():+6.2f}c")
