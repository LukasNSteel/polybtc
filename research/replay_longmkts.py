"""Replay the sniper over 15m / 1h / 4h (and 5m control) markets using the data
downloaded by collect_longmkts.py — the missing piece the 5m-only parquet
archive could never provide.

Per market we reconstruct the live model exactly (bot/fair_value.py + strategy):
  spot / candle-open / vol  : Binance 1s klines (research/data/binance_1s)
  drift                     : momentum_beta 0.5 * recent_return(60s), clamped
  model_p                   : prob_up two-regime mixture (tail 0.25/2.5) + drift
  p_up                      : blend(model_p, market_mid, w=0.65->0.95)
  p_lo,p_hi                 : dual-beta robust bounds (0.83 / 1.36)
  sniper                    : favourite-only [0.50,0.80], net_edge in (min,max],
                              cooldown, per-market + global exposure caps
  outcome                   : GROUND TRUTH from Gamma (outcome_up)

DELIBERATE LIMITATIONS (vs the 1Hz parquet 5m replay):
  * market price is the CLOB 1-MINUTE price-history (UP token), forward-filled to
    1s — so intra-minute book moves are invisible. Coarser than the 5m parquet's
    1s book; fine for the band-level "when is there edge" question, less so for
    sub-minute fill races.
  * no displayed depth in price-history, so size is take*conviction (no book-size
    cap) and fills model race-loss only (+ an optional flat spread on the ask).
Because the SAME fill model is used across every time-gate, the RELATIVE verdict
(which window is best per kind) is robust even though absolute $ are approximate.

Run:  PYTHONPATH=research .venv/bin/python research/replay_longmkts.py
"""
import csv
import gzip
import heapq
from collections import defaultdict

import numpy as np

from replay_binance import load_binance, prob_up_vec, bounds_vec, fee_ps, drawdown

MKT = "research/data/longmkts/markets.csv"
PRICES = "research/data/longmkts/prices.csv.gz"


def load_markets():
    out = []
    with open(MKT) as f:
        for r in csv.DictReader(f):
            out.append((r["kind"], r["slug"], int(r["window_start"]),
                        int(r["window_end"]), int(r["outcome_up"]),
                        float(r["volume"])))
    return out


def load_prices():
    px = defaultdict(list)
    with gzip.open(PRICES, "rt") as f:
        r = csv.reader(f)
        next(r, None)
        for slug, ts, p in r:
            px[slug].append((int(float(ts)), float(p)))
    out = {}
    for slug, pts in px.items():
        pts.sort()
        out[slug] = (np.array([t for t, _ in pts]),
                     np.array([p for _, p in pts]))
    return out


def build_candidates(cfg, base, price, vol, markets, prices):
    """Vectorized per market -> arrays of qualifying snipe candidates."""
    lo, hi = cfg["min_ask"], cfg["max_ask"]
    me, xe = cfg["min_edge"], cfg["max_edge"]
    half_spread = cfg.get("spread", 0.0) / 2.0
    n = len(price)
    rows = []  # (t, end_ts, kind, side, ask, edge, won, t_rem)
    kind_gate = cfg.get("kind_max_t_rem", {})
    kind_gate_min = cfg.get("kind_min_t_rem", {})
    for kind, slug, ws, we, up_won, vol_usd in markets:
        if slug not in prices:
            continue
        if ws < base or we > base + n:
            continue
        # 1s grid over the window; step lets us thin work without losing late detail
        ts = np.arange(ws, we)
        ix = ts - base
        spot = price[ix]
        vv = vol[ix]
        openp = price[ws - base]
        t_rem = (we - ts).astype(float)
        # market mid (UP token) forward-filled from 1-min price history
        pt, pv = prices[slug]
        j = np.searchsorted(pt, ts, side="right") - 1
        valid = j >= 0
        mid_up = np.where(valid, pv[np.clip(j, 0, len(pv) - 1)], np.nan)
        # model
        rr = np.log(spot / price[np.clip(ix - 60, 0, n - 1)])
        drift = 0.5 * rr * np.minimum(t_rem, 60) / 60
        clamp = 1.5 * vv * np.sqrt(np.maximum(t_rem, 0))
        drift = np.clip(drift, -clamp, clamp)
        model_p = prob_up_vec(spot, openp, vv, t_rem, drift)
        w = 0.65 + 0.30 * np.exp(-np.maximum(t_rem, 0) / 60)
        p_up = w * model_p + (1 - w) * mid_up
        p_lo, p_hi = bounds_vec(spot, openp, vv, t_rem)
        ask_up = mid_up + half_spread
        ask_dn = (1 - mid_up) + half_spread
        edge_up = p_lo - ask_up - fee_ps(ask_up)
        edge_dn = (1 - p_hi) - ask_dn - fee_ps(ask_dn)
        cand_up = valid & (ask_up >= lo) & (ask_up <= hi) & (edge_up > me) & (edge_up <= xe)
        cand_dn = valid & (ask_dn >= lo) & (ask_dn <= hi) & (edge_dn > me) & (edge_dn <= xe)
        # global + per-kind late-window gate
        if cfg.get("max_t_rem"):
            keep = t_rem <= cfg["max_t_rem"]; cand_up &= keep; cand_dn &= keep
        if cfg.get("min_t_rem"):
            keep = t_rem >= cfg["min_t_rem"]; cand_up &= keep; cand_dn &= keep
        if kind in kind_gate:
            keep = t_rem <= kind_gate[kind]; cand_up &= keep; cand_dn &= keep
        if kind in kind_gate_min:
            keep = t_rem >= kind_gate_min[kind]; cand_up &= keep; cand_dn &= keep
        for side, cand, ask, edge, won in (
                ("up", cand_up, ask_up, edge_up, up_won),
                ("dn", cand_dn, ask_dn, edge_dn, 1 - up_won)):
            idx = np.where(cand)[0]
            for k in idx:
                rows.append((int(ts[k]), we, kind, side, float(ask[k]),
                             float(edge[k]), int(won), float(t_rem[k])))
    rows.sort(key=lambda r: r[0])
    return rows


def simulate(cfg, cand):
    """Sequential fill sim with cooldown + per-market & global exposure caps."""
    me = cfg["min_edge"]
    cd = cfg.get("cooldown", 10)
    race = cfg.get("race_loss", 0.20)
    max_take = cfg["max_take_usd"]
    max_pos = cfg["max_position_usd"]
    max_exp = cfg["max_exposure_usd"]
    rng = np.random.default_rng(cfg.get("seed", 7))
    pend = []           # (settle_ts, pnl, cost)
    realized = exposure = 0.0
    mkt_cost = defaultdict(float)
    last_fire = {}
    fills = []
    for t, end_ts, kind, side, ask, edge, won, t_rem in cand:
        while pend and pend[0][0] <= t:
            _, pnl, cost = heapq.heappop(pend)
            realized += pnl; exposure -= cost
        key = (end_ts, kind, side)         # one market-window identity
        mkey = (end_ts, kind)
        if t - last_fire.get(key, -1e9) < cd:
            continue
        conv = min(1.0, edge / (2 * me))
        want = max_take * max(0.25, conv)
        want = min(want, max_exp - exposure, max_pos - mkt_cost[mkey])
        if want <= 1.0:
            continue
        last_fire[key] = t
        if rng.random() < race:
            continue
        sh = want / ask
        cost = sh * ask
        fee = sh * fee_ps(ask)
        pnl = sh * won - cost - fee
        heapq.heappush(pend, (end_ts, pnl, cost))
        exposure += cost
        mkt_cost[mkey] += cost
        fills.append((cost, fee, pnl, won, ask, side, int(end_ts), 0.0, kind, t_rem))
    while pend:
        _, pnl, cost = heapq.heappop(pend)
        realized += pnl
    return fills


def run_long(cfg, data):
    base, price, vol, markets, prices = data
    cand = build_candidates(cfg, base, price, vol, markets, prices)
    return simulate(cfg, cand)


def load_all():
    print("loading binance 1s klines...", flush=True)
    base, price, vol, _ = load_binance()
    print(f"  coverage {len(price):,}s", flush=True)
    print("loading markets + prices...", flush=True)
    markets = load_markets()
    prices = load_prices()
    print(f"  {len(markets):,} markets, {len(prices):,} with price history", flush=True)
    return base, price, vol, markets, prices


if __name__ == "__main__":
    data = load_all()
    SHARED = dict(min_ask=0.50, max_ask=0.80, min_edge=0.10, max_edge=0.20,
                  max_take_usd=16, max_position_usd=32, max_exposure_usd=100,
                  cooldown=10, race_loss=0.20)
    fills = run_long(SHARED, data)
    bk = defaultdict(list)
    for f in fills:
        bk[f[8]].append(f)
    print(f"\n{'kind':6} {'fills':>6} {'win%':>6} {'dep$':>9} {'pnl$':>8} "
          f"{'ROI/$':>7} {'maxDD$':>7}")
    for k in ["5m", "15m", "1h", "4h"]:
        fl = bk[k]
        if not fl:
            print(f"{k:6} {'0':>6}"); continue
        dep = sum(f[0] for f in fl); pnl = sum(f[2] for f in fl)
        win = np.mean([f[3] for f in fl]); mdd = drawdown(fl)
        print(f"{k:6} {len(fl):>6} {win:>6.1%} {dep:>9.0f} {pnl:>+8.0f} "
              f"{pnl/dep:>+7.2%} {mdd:>7.0f}")
