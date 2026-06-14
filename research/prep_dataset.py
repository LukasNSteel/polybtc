"""Build enriched analysis datasets from collected raw data.

Outputs:
  research/data/taker_trades.parquet
      one row per taker execution, with: side bought, price paid, fee,
      outcome, time-to-expiry, replicated model fair value (no lookahead),
      momentum/distance/flow features, and forward markouts.
  research/data/snapshots.parquet
      one row per (market, tau gridpoint): last trade price, model_p, outcome.
"""

import gzip
import math

import numpy as np
import pandas as pd

D = "research/data"

TAIL_W, TAIL_S = 0.25, 2.5
MIN_VOL = 2e-5
MOM_WINDOW, MOM_BETA, MOM_CLAMP = 60, 0.5, 1.5
FEE_RATE = 0.07

TAU_GRID = {
    "5m": [290, 270, 240, 210, 180, 150, 120, 90, 60, 45, 30, 20, 10, 5],
    "15m": [890, 840, 780, 720, 660, 600, 540, 480, 420, 360, 300, 240, 180, 120, 90, 60, 45, 30, 20, 10, 5],
    "1h": [3590, 3300, 3000, 2700, 2400, 2100, 1800, 1500, 1200, 900, 600, 450, 300, 180, 120, 60, 30, 10],
    "4h": [14300, 13200, 12000, 10800, 9600, 8400, 7200, 6000, 4800, 3600, 2700, 1800, 1200, 600, 300, 120, 60],
}


def norm_cdf(x):
    return 0.5 * (1.0 + np.vectorize(math.erf)(x / math.sqrt(2.0)))


def prob_up_vec(spot, open_price, vol, tau, drift):
    d = (np.log(spot / open_price) + drift) / (vol * np.sqrt(np.maximum(tau, 1e-9)))
    p = (1 - TAIL_W) * norm_cdf(d) + TAIL_W * norm_cdf(d / TAIL_S)
    return np.where(tau <= 0, (spot >= open_price).astype(float), p)


def load():
    mk = pd.read_csv(f"{D}/markets.csv")
    tr = pd.read_csv(f"{D}/trades.csv.gz")
    b1s = pd.read_csv(f"{D}/binance_1s.csv.gz")
    b1m = pd.read_csv(f"{D}/binance_1m.csv.gz")
    return mk, tr, b1s, b1m


def build_binance_state(b1s, b1m):
    """Per-second (and per-minute) spot, EWMA vol (per-sec units), 60s momentum."""
    out = {}
    for name, df, dt in [("1s", b1s, 1), ("1m", b1m, 60)]:
        df = df.sort_values("ts").drop_duplicates("ts").reset_index(drop=True)
        c = df["close"].astype(float)
        r = np.log(c / c.shift(1))
        # EWMA variance of per-bar returns, halflives 60s and 600s (in bars)
        v_fast = r.pow(2).ewm(halflife=max(60 / dt, 1)).mean()
        v_slow = r.pow(2).ewm(halflife=max(600 / dt, 1)).mean()
        vol_bar = np.sqrt(np.maximum(v_fast, v_slow))
        vol_sec = np.maximum(vol_bar / math.sqrt(dt), MIN_VOL)
        nback = max(MOM_WINDOW // dt, 1)
        mom = np.log(c / c.shift(nback))
        out[name] = pd.DataFrame({
            "ts": df["ts"], "close": c, "open_bar": df["open"].astype(float),
            "vol_sec": vol_sec, "mom60": mom,
        })
    return out


def asof_join(left, right, on="ts", cols=None, tolerance=None):
    lf = left.sort_values(on).reset_index()
    rf = right.sort_values(on)
    j = pd.merge_asof(lf, rf[[on] + cols], on=on, direction="backward",
                      tolerance=tolerance)
    return j.set_index("index").sort_index()


def main():
    mk, tr, b1s, b1m = load()
    print(f"markets={len(mk)} trades={len(tr)}")
    bs = build_binance_state(b1s, b1m)
    b1s_state, b1m_state = bs["1s"], bs["1m"]
    ts1s_min, ts1s_max = b1s_state.ts.min(), b1s_state.ts.max()

    mk = mk.set_index("slug")
    tr = tr.merge(mk[["window_start", "window_end", "outcome_up"]],
                  left_on="slug", right_index=True, how="inner")
    tr = tr[(tr.ts >= tr.window_start) & (tr.ts <= tr.window_end)].copy()
    print(f"in-window trades: {len(tr)}")

    # normalize every execution into "taker bought side X at price q"
    is_buy = tr["side"] == "BUY"
    is_up = tr["outcome"] == "Up"
    tr["bought_up"] = (is_buy & is_up) | (~is_buy & ~is_up)
    tr["q"] = np.where(is_buy, tr.price, 1 - tr.price)          # price paid for bought side
    tr["p_up_trade"] = np.where(is_up, tr.price, 1 - tr.price)  # implied P(up) of the print
    tr["notional"] = tr["size"] * tr["q"]
    tr["tau"] = tr.window_end - tr.ts
    tr["y_up"] = tr.outcome_up.astype(int)
    tr["y"] = np.where(tr.bought_up, tr.y_up, 1 - tr.y_up)
    tr["fee"] = FEE_RATE * tr.q * (1 - tr.q)

    # binance features at trade time (use bar ending strictly before ts: shift ts by -1)
    tr["ts_q"] = tr.ts - 1
    use1s = (tr.ts_q >= ts1s_min + 600) & (tr.ts_q <= ts1s_max)
    parts = []
    for sel, state, tol in [(use1s, b1s_state, 5), (~use1s, b1m_state, 120)]:
        part = tr[sel].copy()
        if not len(part):
            continue
        j = pd.merge_asof(part.sort_values("ts_q"), state.rename(columns={"ts": "ts_q"}),
                          on="ts_q", direction="backward", tolerance=tol)
        part = j
        parts.append(part)
    tr = pd.concat(parts, ignore_index=True)
    tr = tr.dropna(subset=["close", "vol_sec"])

    # window open price: 1s bar open at window_start if covered, else 1m bar open
    opens_1s = b1s_state.set_index("ts")["open_bar"]
    opens_1m = b1m_state.set_index("ts")["open_bar"]
    ws = tr["window_start"]
    o1 = ws.map(opens_1s)
    o2 = ws.map(opens_1m)
    tr["open_price"] = o1.fillna(o2)
    tr = tr.dropna(subset=["open_price"])

    # replicated model fair value with momentum drift
    sig_sqrt_tau = tr.vol_sec * np.sqrt(np.maximum(tr.tau, 1e-9))
    drift = MOM_BETA * tr.mom60.fillna(0) * np.minimum(tr.tau, MOM_WINDOW) / MOM_WINDOW
    drift = np.clip(drift, -MOM_CLAMP * sig_sqrt_tau, MOM_CLAMP * sig_sqrt_tau)
    tr["model_p_up"] = prob_up_vec(tr.close, tr.open_price, tr.vol_sec, tr.tau, drift)
    tr["model_p_side"] = np.where(tr.bought_up, tr.model_p_up, 1 - tr.model_p_up)
    tr["dist_z"] = np.log(tr.close / tr.open_price) / sig_sqrt_tau
    tr["mom_z"] = tr.mom60.fillna(0) / sig_sqrt_tau

    # prior market price (last print before this one, same market)
    tr = tr.sort_values(["slug", "ts"]).reset_index(drop=True)
    tr["p_up_prev"] = tr.groupby("slug")["p_up_trade"].shift(1)

    # signed taker flow (UP-equivalent notional), rolling 60s per market
    tr["flow"] = np.where(tr.bought_up, tr.notional, -tr.notional)
    flows = []
    for slug, g in tr.groupby("slug", sort=False):
        s = pd.Series(g.flow.values, index=pd.to_datetime(g.ts.values, unit="s"))
        flows.append(pd.Series(s.rolling("60s").sum().values, index=g.index))
    tr["flow60"] = pd.concat(flows).sort_index()

    # forward markouts: market p_up at t+10s / t+60s (last print before horizon)
    n = len(tr)
    for col, h in [("p_up_10s", 10), ("p_up_60s", 60)]:
        vals = np.full(n, np.nan)
        for slug, g in tr.groupby("slug", sort=False):
            ts = g.ts.values
            pu = g.p_up_trade.values
            idx = np.searchsorted(ts, ts + h, side="right") - 1
            vals[g.index] = pu[idx]
        tr[col] = vals

    keep = ["kind", "slug", "ts", "tau", "bought_up", "q", "size", "notional", "fee",
            "p_up_trade", "p_up_prev", "y", "y_up", "model_p_up", "model_p_side",
            "dist_z", "mom_z", "flow60", "p_up_10s", "p_up_60s", "vol_sec",
            "close", "open_price"]
    tt = tr[keep]
    tt.to_parquet(f"{D}/taker_trades.parquet", index=False)
    print(f"taker_trades: {len(tt)} rows -> parquet")

    # snapshot frame: per market x tau gridpoint, last print + model state
    snaps = []
    for slug, g in tr.groupby("slug", sort=False):
        kind = g.kind.iloc[0]
        we = g.window_end.iloc[0]
        ts = g.ts.values
        for tau in TAU_GRID[kind]:
            t = we - tau
            i = np.searchsorted(ts, t, side="right") - 1
            if i < 0:
                continue
            row = g.iloc[i]
            age = t - ts[i]
            if age > max(0.2 * tau, 10):
                continue
            snaps.append({
                "kind": kind, "slug": slug, "tau": tau, "p_up": row.p_up_trade,
                "age": age, "y_up": int(row.y_up), "model_p_up": row.model_p_up,
                "dist_z": row.dist_z, "mom_z": row.mom_z, "flow60": row.flow60,
                "vol_sec": row.vol_sec,
            })
    sn = pd.DataFrame(snaps)
    sn.to_parquet(f"{D}/snapshots.parquet", index=False)
    print(f"snapshots: {len(sn)} rows -> parquet")
    print(sn.groupby('kind').slug.nunique())


if __name__ == "__main__":
    main()
