"""Faithful-as-possible replay of the live sniper over historical data.

Joins:
  - Binance 1s BTCUSDT klines (data.binance.vision)  -> spot, candle open, vol
  - Polymarket book ticks (btc_ticks.parquet)        -> bid/ask/size per second
  - Market outcomes (btc_markets.parquet)            -> settlement (ground truth)

It re-implements the exact model from bot/fair_value.py + bot/strategy.py:
  vol_per_sec : EWMA of r^2 (fast hl=60s, slow hl=600s), floored at 2e-5, take max
  drift       : momentum_beta * recent_return(60s) * min(t_rem,60)/60, clamped
  model_p     : prob_up (two-regime Gaussian mixture, tail 0.25/2.5) with drift
  p_up        : blend_with_market(model_p, book_mid, t_rem, w=0.65->0.95)
  p_lo,p_hi   : prob_up_bounds (dual-beta 0.83 / 1.36)
  sniper      : favourite-only [0.50,0.80], net_edge = robust - ask - fee(ask),
                gate min_edge<edge<=max_edge, size = max_take*conviction capped
                by displayed size, per-market cap, exposure cap, cooldown.
  fills       : paper frictions — race-loss prob 0.20, capture 0.30 of displayed.
  fee         : 0.07 * p * (1-p) per share.

LIMITATION: this runs at 1 Hz (the parquet/kline cadence). The live speed-bump
adverse selection is a SUB-second effect, so this still slightly overstates
realized edge (same caveat the team's prior backtests note). The race-loss +
capture haircut approximate it; treat results as an upper-ish bound, validated
against the paper sessions.
"""
import glob
import io
import sys
import zipfile
from collections import defaultdict

import numpy as np
import pandas as pd
from scipy.special import ndtr

ARCH = "/Users/lukassteel/Desktop/archive (1)"
KDIR = "research/data/binance_1s"
FEE = 0.07
MIN_VOL = 2e-5


# ---------------------------------------------------------------- binance load
def load_binance(obi_halflife=20.0):
    """Returns base second, forward-filled 1s price, vol (EWMA of r^2), and an
    EWMA-smoothed Binance order-FLOW imbalance (OFI/aggressor imbalance) in
    [-1,1]: +1 = all taker buys that second, -1 = all taker sells. Derived from
    kline cols 5 (base volume) and 9 (taker-buy base volume) — a short-horizon
    lead signal that complements the price lead-lag (recent return) drift term."""
    times, closes, volb, tbb = [], [], [], []
    for z in sorted(glob.glob(f"{KDIR}/*.zip")):
        with zipfile.ZipFile(z) as zf:
            raw = zf.read(zf.namelist()[0])
        a = np.loadtxt(io.BytesIO(raw), delimiter=",", usecols=(0, 4, 5, 9))
        times.append(a[:, 0]); closes.append(a[:, 1])
        volb.append(a[:, 2]); tbb.append(a[:, 3])
    ot = np.concatenate(times)
    cl = np.concatenate(closes)
    vb = np.concatenate(volb)
    tb = np.concatenate(tbb)
    sec = (ot // 1_000_000).astype(np.int64)
    order = np.argsort(sec)
    sec, cl, vb, tb = sec[order], cl[order], vb[order], tb[order]
    _, uniq = np.unique(sec, return_index=True)
    sec, cl, vb, tb = sec[uniq], cl[uniq], vb[uniq], tb[uniq]
    base = sec[0]
    n = sec[-1] - base + 1
    price = np.full(n, np.nan)
    price[sec - base] = cl
    # forward-fill price gaps
    price = price[np.maximum.accumulate(np.where(np.isnan(price), 0, np.arange(len(price))))]
    # per-second flow imbalance on the full grid (0 where no trades / gap)
    volg = np.zeros(n); tbg = np.zeros(n)
    volg[sec - base] = vb; tbg[sec - base] = tb
    with np.errstate(divide="ignore", invalid="ignore"):
        ofi_raw = np.where(volg > 0, (2 * tbg - volg) / volg, 0.0)
    ofi_raw = np.clip(ofi_raw, -1.0, 1.0)
    # vol: EWMA of r^2 with fast/slow halflives (dt=1s); obi: EWMA of ofi_raw
    r = np.zeros(len(price))
    r[1:] = np.log(price[1:] / price[:-1])
    samp = r * r
    af, as_ = 1 - 0.5 ** (1 / 60), 1 - 0.5 ** (1 / 600)
    ao = 1 - 0.5 ** (1 / max(obi_halflife, 1.0))
    vf = np.empty(len(samp)); vs = np.empty(len(samp)); obi = np.empty(n)
    cf = cs = samp[0]; co = ofi_raw[0]
    for i in range(len(samp)):
        cf += af * (samp[i] - cf)
        cs += as_ * (samp[i] - cs)
        co += ao * (ofi_raw[i] - co)
        vf[i] = cf; vs[i] = cs; obi[i] = co
    vol = np.maximum(np.sqrt(np.maximum(vf, vs)), MIN_VOL)
    return base, price, vol, obi


# ----------------------------------------------------------------- ticks load
def load_ticks():
    m = pd.read_parquet(f"{ARCH}/btc_markets.parquet",
                        columns=["condition_id", "market_start", "market_end", "outcome"])
    m = m[m.outcome.isin(["Up", "Down"])].copy()
    m["start_ep"] = m.market_start.astype("int64") // 10**9
    m["end_ep"] = m.market_end.astype("int64") // 10**9
    m["up_won"] = (m.outcome == "Up").astype(np.int8)
    t = pd.read_parquet(f"{ARCH}/btc_ticks.parquet",
                        columns=["condition_id", "t", "bu", "au", "bd", "ad", "sau", "sad"])
    t = t.merge(m[["condition_id", "start_ep", "end_ep", "up_won"]], on="condition_id")
    # next-tick book per market = the "fresher book" the live executor re-validates
    # against during the speed-bump hold (feed_lag) and uses for edge_contention.
    t = t.sort_values(["condition_id", "t"])
    g = t.groupby("condition_id")
    t["midu"] = (t.bu + t.au) / 2
    t["midd"] = (t.bd + t.ad) / 2
    t["au_next"] = g["au"].shift(-1)
    t["ad_next"] = g["ad"].shift(-1)
    t["midu_next"] = g["midu"].shift(-1)
    t["midd_next"] = g["midd"].shift(-1)
    t["nv"] = ((g["t"].shift(-1) - t["t"]).between(1, 3)).astype(float)
    for c in ["au_next", "ad_next", "midu_next", "midd_next"]:
        t[c] = t[c].fillna(0.0)
    t = t.sort_values("t", kind="stable")
    ticks = {c: t[c].values for c in
             ["condition_id", "t", "bu", "au", "bd", "ad", "sau", "sad",
              "start_ep", "end_ep", "up_won",
              "au_next", "ad_next", "midu_next", "midd_next", "nv"]}
    return ticks


# ---------------------------------------------------------------- model (vec)
def prob_up_vec(spot, openp, vol, t_rem, drift, tw=0.25, ts=2.5):
    sd = vol * np.sqrt(np.maximum(t_rem, 1e-9))
    d = (np.log(spot / openp) + drift) / sd
    p = (1 - tw) * ndtr(d) + tw * ndtr(d / ts)
    return np.where(t_rem <= 0, (spot >= openp).astype(float), p)


def bounds_vec(spot, openp, vol, t_rem, betas=(0.83, 1.36), tw=0.25, ts=2.5):
    sd = vol * np.sqrt(np.maximum(t_rem, 1e-9))
    d = np.log(spot / openp) / sd
    ps = [(1 - tw) * ndtr(b * d) + tw * ndtr(b * d / ts) for b in betas]
    return np.minimum.reduce(ps), np.maximum.reduce(ps)


def fee_ps(a):
    return FEE * a * (1 - a)


# Canonical BTC up/down window lengths (seconds) -> kind label. Each market's
# kind is recovered from its actual duration (end_ep - start_ep) snapped to the
# nearest of these, so the replay can gate / attribute PnL per market kind.
KIND_DUR = {"5m": 300, "15m": 900, "1h": 3600, "4h": 14400}


def classify_kind(dur):
    """Vectorized: snap each window duration (s) to the nearest canonical kind."""
    dur = np.asarray(dur, dtype=float)
    labels = np.array(list(KIND_DUR.keys()))
    durs = np.array(list(KIND_DUR.values()), dtype=float)
    # nearest in log space so 300 vs 900 vs 3600 vs 14400 separate cleanly
    idx = np.argmin(np.abs(np.log(dur[:, None]) - np.log(durs[None, :])), axis=1)
    return labels[idx]


# ---------------------------------------------------------------- features
def compute_features(cfg, base, price, vol, ticks, obi=None):
    """Re-implement the live model over every in-window book tick and return the
    full feature/candidate arrays. Shared by run() (the fill simulator) and the
    distance-gate diagnostic, so both use IDENTICAL model math."""
    s = ticks["t"]
    inb = (s >= base) & (s < base + len(price))
    d = {k: v[inb] for k, v in ticks.items()}
    s = d["t"]
    ix = s - base
    spot = price[ix]
    vv = vol[ix]
    ob = obi[ix] if obi is not None else np.zeros(len(ix))
    openp = price[np.clip(d["start_ep"] - base, 0, len(price) - 1)]
    t_rem = (d["end_ep"] - s).astype(float)
    kind = classify_kind(d["end_ep"] - d["start_ep"])
    rr = np.log(spot / price[np.clip(ix - 60, 0, len(price) - 1)])

    # DISTANCE-TO-STRIKE features (the physical state, independent of the model
    # edge): how far spot has already moved from the candle open, expressed in
    # standard deviations of the remaining-horizon move (signed; >0 favors UP)
    # and in raw dollars (signed; >0 favors UP). dist_sigma is exactly the `d`
    # that drives prob_up in bot/fair_value.py (no drift term — the pure state).
    sd = vv * np.sqrt(np.maximum(t_rem, 1e-9))
    dist_sigma = np.log(spot / openp) / sd
    dist_usd = spot - openp

    mom_beta = 0.5
    drift = mom_beta * rr * np.minimum(t_rem, 60) / 60
    # OFI drift tilt: combine the flow imbalance with the price lead-lag. Scaled
    # into log-price units by current vol so it is comparable to the momentum term.
    if cfg.get("obi_tilt"):
        drift = drift + cfg["obi_tilt"] * ob * vv * np.minimum(t_rem, 60)
    clamp = 1.5 * vv * np.sqrt(np.maximum(t_rem, 0))
    drift = np.clip(drift, -clamp, clamp)

    model_p = prob_up_vec(spot, openp, vv, t_rem, drift)
    mid_up = (d["bu"] + d["au"]) / 2
    w = 0.65 + 0.30 * np.exp(-np.maximum(t_rem, 0) / 60)
    p_up = w * model_p + (1 - w) * mid_up
    p_lo, p_hi = bounds_vec(spot, openp, vv, t_rem)
    # tilt the robust bounds too, so edges shift consistently with the signal
    if cfg.get("obi_tilt"):
        delta = np.clip(cfg.get("obi_bound_gain", 0.10) * ob, -0.05, 0.05)
        p_lo = np.clip(p_lo + delta, 0.0, 1.0)
        p_hi = np.clip(p_hi + delta, 0.0, 1.0)

    au, ad = d["au"], d["ad"]
    edge_up = p_lo - au - fee_ps(au)
    edge_dn = (1 - p_hi) - ad - fee_ps(ad)

    lo, hi = cfg["min_ask"], cfg["max_ask"]
    me, xe = cfg["min_edge"], cfg["max_edge"]
    cand_up = (au >= lo) & (au <= hi) & (edge_up > me) & (edge_up <= xe)
    cand_dn = (ad >= lo) & (ad <= hi) & (edge_dn > me) & (edge_dn <= xe)
    # OFI directional gate: only snipe a side when Binance flow agrees with it.
    if cfg.get("obi_gate") is not None:
        thr = cfg["obi_gate"]
        cand_up &= ob >= thr
        cand_dn &= ob <= -thr
    # late-window gate (optional): only snipe inside [min_t_rem, max_t_rem] seconds
    if cfg.get("max_t_rem"):
        cand_up &= t_rem <= cfg["max_t_rem"]
        cand_dn &= t_rem <= cfg["max_t_rem"]
    if cfg.get("min_t_rem"):
        cand_up &= t_rem >= cfg["min_t_rem"]
        cand_dn &= t_rem >= cfg["min_t_rem"]
    # PER-KIND late-window gate (optional): {kind: max_t_rem_sec}. A kind listed
    # here may only fire inside its last max_t_rem_sec; kinds NOT listed keep the
    # full window. This is exactly the "betting window for 15m/1h/4h, leave 5m
    # full" rule under test. kind_min_t_rem mirrors it on the lower bound.
    for key, cmp in (("kind_max_t_rem", "le"), ("kind_min_t_rem", "ge")):
        gate = cfg.get(key)
        if not gate:
            continue
        for k, lim in gate.items():
            km = kind == k
            ok = (t_rem <= lim) if cmp == "le" else (t_rem >= lim)
            keep = ~km | ok  # outside this kind -> untouched; inside -> must pass
            cand_up &= keep
            cand_dn &= keep
    # DISTANCE-TO-STRIKE gate (the hypothesis under test): only fire when spot
    # has already moved in the bet's favour by >= this many sigma and/or dollars.
    if cfg.get("dist_sigma_min"):
        th = cfg["dist_sigma_min"]
        cand_up &= dist_sigma >= th
        cand_dn &= dist_sigma <= -th
    if cfg.get("dist_usd_min"):
        th = cfg["dist_usd_min"]
        cand_up &= dist_usd >= th
        cand_dn &= dist_usd <= -th

    return dict(s=s, d=d, ob=ob, t_rem=t_rem, kind=kind, spot=spot, openp=openp,
                p_up=p_up, p_lo=p_lo, p_hi=p_hi, au=au, ad=ad,
                edge_up=edge_up, edge_dn=edge_dn, cand_up=cand_up, cand_dn=cand_dn,
                dist_sigma=dist_sigma, dist_usd=dist_usd)


# ---------------------------------------------------------------- main
def run(cfg, base, price, vol, ticks, obi=None):
    F = compute_features(cfg, base, price, vol, ticks, obi)
    s, d = F["s"], F["d"]
    ob, t_rem, kind = F["ob"], F["t_rem"], F["kind"]
    au, ad = F["au"], F["ad"]
    edge_up, edge_dn = F["edge_up"], F["edge_dn"]
    cand_up, cand_dn = F["cand_up"], F["cand_dn"]
    cand = cand_up | cand_dn
    ci = np.where(cand)[0]
    me = cfg["min_edge"]  # used by the conviction sizing below

    rng = np.random.default_rng(cfg.get("seed", 7))
    race = cfg.get("race_loss", 0.20)
    cap = cfg.get("capture", 0.30)
    # paper fill-realism extensions (mirror bot/execution.py)
    contention = bool(cfg.get("contention", False))
    feedlag = bool(cfg.get("feedlag", False))
    CONTENTION_SCALE = 0.10          # capture -> 0 at 10c below the fresher mid
    SLACK = cfg.get("limit_slack", 0.02)   # 2-tick FAK limit slack
    au_next, ad_next = d["au_next"], d["ad_next"]
    midu_next, midd_next = d["midu_next"], d["midd_next"]
    nv = d["nv"]
    start_cash = 1000.0
    realized = 0.0
    import heapq
    pend = []                       # (end_ep, pnl, cost)
    exposure = 0.0
    mkt_cost = defaultdict(float)
    last_fire = {}                  # (cond, side) -> t
    fills = []

    cond = d["condition_id"]; end_ep = d["end_ep"]; uw = d["up_won"]
    sau, sad = d["sau"], d["sad"]

    for i in ci:
        ti = s[i]
        while pend and pend[0][0] <= ti:
            _, pnl, cost = heapq.heappop(pend)
            realized += pnl; exposure -= cost
        equity = start_cash + realized
        if cfg.get("no_scale"):
            scale = 1.0
        else:
            scale = max(0.5, min(np.sqrt(equity / 1000.0), cfg.get("scale_cap", 5.0)))
        max_take = min(cfg["max_take_usd"] * scale, cfg.get("take_cap", 500))
        max_pos = min(cfg["max_position_usd"] * scale, cfg.get("pos_cap", 500))
        max_exp = min(cfg["max_exposure_usd"] * scale, cfg.get("exp_cap", 500))

        for side in ("up", "dn"):
            ok = cand_up[i] if side == "up" else cand_dn[i]
            if not ok:
                continue
            key = (cond[i], side)
            if ti - last_fire.get(key, -1e9) < cfg.get("cooldown", 10):
                continue
            ask = au[i] if side == "up" else ad[i]
            edge = edge_up[i] if side == "up" else edge_dn[i]
            disp = sau[i] if side == "up" else sad[i]
            conv = min(1.0, edge / (2 * me))
            want = min(max_take * max(0.25, conv), ask * disp)
            if exposure + want > max_exp or mkt_cost[cond[i]] + want > max_pos:
                want = min(want, max_exp - exposure, max_pos - mkt_cost[cond[i]])
            if want <= 1.0:
                continue
            last_fire[key] = ti
            if rng.random() < race:          # FAK lost the race
                continue
            cap_eff = cap
            fill_ask = ask
            if nv[i]:
                ask1 = au_next[i] if side == "up" else ad_next[i]
                mid1 = midu_next[i] if side == "up" else midd_next[i]
                # feed_lag: our WS book trails the engine; we re-validate against
                # a fresher book. A favourable move that richened the quote past
                # our limit (+slack) is a miss — we lose exactly the best fills.
                if feedlag and ask1 > fill_ask + SLACK + 1e-9:
                    continue
                # edge_contention: the cheaper the stale ask vs the fresher mid,
                # the more contested the quote, so we win less of the displayed size.
                if contention and mid1 > 0:
                    discount = max(0.0, mid1 - fill_ask)
                    cap_eff = cap * (1.0 - min(1.0, discount / CONTENTION_SCALE))
            if cap_eff <= 0:
                continue
            fill_sh = min(want / ask, cap_eff * disp)
            if fill_sh < 1:
                continue
            cost = fill_sh * ask
            f = fill_sh * fee_ps(ask)
            won = uw[i] if side == "up" else (1 - uw[i])
            pnl = fill_sh * won - cost - f
            heapq.heappush(pend, (end_ep[i], pnl, cost))
            exposure += cost
            mkt_cost[cond[i]] += cost
            # tuple fields 0..7 unchanged for back-compat; 8=kind, 9=t_rem(s)
            fills.append((cost, f, pnl, won, ask, side, int(end_ep[i]), float(ob[i]),
                          str(kind[i]), float(t_rem[i])))
    while pend:
        _, pnl, cost = heapq.heappop(pend)
        realized += pnl
    return fills, realized


def drawdown(fills, start=1000.0):
    if not fills:
        return 0.0
    order = sorted(fills, key=lambda f: f[6])
    eq = start
    peak = start
    mdd = 0.0
    for f in order:
        eq += f[2]
        peak = max(peak, eq)
        mdd = max(mdd, peak - eq)
    return mdd


def main():
    print("loading binance 1s klines...", flush=True)
    base, price, vol, obi = load_binance()
    print(f"  binance coverage: {len(price):,}s "
          f"({pd.to_datetime(base, unit='s')} .. {pd.to_datetime(base+len(price), unit='s')})")

    print("loading parquet book + outcomes...", flush=True)
    ticks = load_ticks()
    print(f"  ticks: {len(ticks['t']):,}\n")

    # FIXED CAPS (no equity compounding) so $ and ROI are interpretable and
    # comparable; ROI/$ deployed is the friction-robust efficiency metric.
    # base_cfg now models the FULL paper fill realism: race-loss + capture +
    # edge_contention + feed_lag (mirrors bot/execution.py). This de-inflates the
    # absolute numbers and gives a fair verdict on max_edge (richer quotes are
    # more contested, so they no longer look "free").
    base_cfg = dict(min_ask=0.50, max_ask=0.80, min_edge=0.10, max_edge=0.25,
                    max_take_usd=30, max_position_usd=60, max_exposure_usd=200,
                    cooldown=10, race_loss=0.20, capture=0.30,
                    contention=True, feedlag=True, no_scale=True)
    scenarios = {
        # --- contrast: old frictionless-ish model vs the new realistic one ---
        "OLD model (race+capture only), 1000-cfg":
            dict(max_edge=0.25, max_take_usd=100, max_position_usd=150,
                 max_exposure_usd=500, contention=False, feedlag=False),
        "NEW frictions, 1000-cfg (edge.25, take100/exp500)":
            dict(max_edge=0.25, max_take_usd=100, max_position_usd=150,
                 max_exposure_usd=500),
        # --- max_edge verdict, both under the new frictions ---
        "OPT caps, edge.25 (take30/pos60/exp200)": dict(max_edge=0.25),
        "OPT caps, edge.20": dict(max_edge=0.20),
        "OPT caps, edge.18": dict(max_edge=0.18),
        # --- late-window gate, on top of OPT caps + edge.20 ---
        "OPT.20 + late t_rem in [10,120]s": dict(max_edge=0.20, min_t_rem=10, max_t_rem=120),
        "OPT.20 + late t_rem in [10,90]s": dict(max_edge=0.20, min_t_rem=10, max_t_rem=90),
        "OPT.20 + late t_rem in [15,90]s": dict(max_edge=0.20, min_t_rem=15, max_t_rem=90),
        "OPT.20 + late t_rem in [10,60]s": dict(max_edge=0.20, min_t_rem=10, max_t_rem=60),
    }
    print(f"{'scenario (fixed caps, no compounding)':50} {'fills':>6} {'win%':>6} "
          f"{'dep$':>9} {'pnl$':>8} {'ROI/$':>7} {'maxDD$':>7} {'pnl/DD':>7}")
    print("-" * 108)
    for name, override in scenarios.items():
        cfg = {**base_cfg, **override}
        fills, realized = run(cfg, base, price, vol, ticks, obi=obi)
        if not fills:
            print(f"{name:50} {'0':>6}")
            continue
        dep = sum(f[0] for f in fills)
        pnl = sum(f[2] for f in fills)
        win = np.mean([f[3] for f in fills])
        mdd = drawdown(fills)
        print(f"{name:50} {len(fills):>6} {win:>6.1%} {dep:>9.0f} {pnl:>+8.0f} "
              f"{pnl/dep:>+7.1%} {mdd:>7.0f} {pnl/mdd if mdd else 0:>7.1f}")
    print("-" * 104)
    print("Fixed caps, $1000 start, no equity-scaling. ROI/$ = pnl per $ deployed")
    print("(friction-robust). pnl/DD = total pnl / max drawdown over ~8 weeks.")


if __name__ == "__main__":
    main()
