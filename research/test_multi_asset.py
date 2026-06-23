"""Out-of-sample validation of the $1000 OPTIMISED config (exp100, full window)
across ALL crypto assets in the archive, not just BTC.

The archive has 7 assets in the identical book-tick schema (btc/eth/sol/doge/
xrp/bnb/hype). BTC is what we tuned exp100 on; the other 6 are genuine
out-of-sample data (different markets, ~6 weeks each, Apr 5 - May 18). If
exp100's "2-month drawdown stays under the $1000 bankroll" result holds on the
other assets too, the sizing is robust, not BTC-overfit.

Reuses the faithful replay engine (model + frictions) from replay_binance.py;
only the data loaders are parameterized by asset. Per-asset Binance 1s klines
must live in research/data/binance_1s_<SYMBOL>/ (see the downloader).

Run:  PYTHONPATH=research python research/test_multi_asset.py
"""
import glob
import io
import zipfile

import numpy as np
import pandas as pd

import replay_binance as rb
from replay_binance import run, drawdown

CAPITAL = 1000.0
# the $1000 OPTIMISED config (config.live1000optimised.yaml): exp100, full window
CFG = dict(min_ask=0.50, max_ask=0.80, min_edge=0.10, max_edge=0.20,
           max_take_usd=16, max_position_usd=32, max_exposure_usd=100,
           cooldown=10, race_loss=0.20, capture=0.30,
           contention=True, feedlag=True, no_scale=True)

# asset -> (Binance spot symbol, archive parquet prefix)
ASSETS = [
    ("BTC", "BTCUSDT", "btc"),
    ("ETH", "ETHUSDT", "eth"),
    ("SOL", "SOLUSDT", "sol"),
    ("DOGE", "DOGEUSDT", "doge"),
    ("XRP", "XRPUSDT", "xrp"),
    ("BNB", "BNBUSDT", "bnb"),
    ("HYPE", "HYPEUSDT", "hype"),
]
SEEDS = list(range(1, 13))


def kline_dir(symbol):
    # BTC klines live in the original dir; the rest in per-symbol dirs.
    if symbol == "BTCUSDT":
        return "research/data/binance_1s"
    return f"research/data/binance_1s_{symbol}"


def load_binance_asset(kdir):
    """Same as replay_binance.load_binance but reads an arbitrary kline dir."""
    times, closes, volb, tbb = [], [], [], []
    for z in sorted(glob.glob(f"{kdir}/*.zip")):
        with zipfile.ZipFile(z) as zf:
            raw = zf.read(zf.namelist()[0])
        a = np.loadtxt(io.BytesIO(raw), delimiter=",", usecols=(0, 4, 5, 9))
        times.append(a[:, 0]); closes.append(a[:, 1])
        volb.append(a[:, 2]); tbb.append(a[:, 3])
    if not times:
        return None
    ot = np.concatenate(times); cl = np.concatenate(closes)
    vb = np.concatenate(volb); tb = np.concatenate(tbb)
    sec = (ot // 1_000_000).astype(np.int64)
    order = np.argsort(sec)
    sec, cl, vb, tb = sec[order], cl[order], vb[order], tb[order]
    _, uniq = np.unique(sec, return_index=True)
    sec, cl, vb, tb = sec[uniq], cl[uniq], vb[uniq], tb[uniq]
    base = sec[0]; n = sec[-1] - base + 1
    price = np.full(n, np.nan); price[sec - base] = cl
    price = price[np.maximum.accumulate(np.where(np.isnan(price), 0, np.arange(len(price))))]
    volg = np.zeros(n); tbg = np.zeros(n)
    volg[sec - base] = vb; tbg[sec - base] = tb
    with np.errstate(divide="ignore", invalid="ignore"):
        ofi_raw = np.where(volg > 0, (2 * tbg - volg) / volg, 0.0)
    ofi_raw = np.clip(ofi_raw, -1.0, 1.0)
    r = np.zeros(len(price)); r[1:] = np.log(price[1:] / price[:-1])
    samp = r * r
    af, as_ = 1 - 0.5 ** (1 / 60), 1 - 0.5 ** (1 / 600)
    ao = 1 - 0.5 ** (1 / 20)
    vf = np.empty(len(samp)); vs = np.empty(len(samp)); obi = np.empty(n)
    cf = cs = samp[0]; co = ofi_raw[0]
    for i in range(len(samp)):
        cf += af * (samp[i] - cf); cs += as_ * (samp[i] - cs)
        co += ao * (ofi_raw[i] - co)
        vf[i] = cf; vs[i] = cs; obi[i] = co
    vol = np.maximum(np.sqrt(np.maximum(vf, vs)), rb.MIN_VOL)
    return base, price, vol, obi


def load_ticks_asset(prefix):
    """Same as replay_binance.load_ticks but for an arbitrary archive prefix."""
    m = pd.read_parquet(f"{rb.ARCH}/{prefix}_markets.parquet",
                        columns=["condition_id", "market_start", "market_end", "outcome"])
    m = m[m.outcome.isin(["Up", "Down"])].copy()
    m["start_ep"] = m.market_start.astype("int64") // 10**9
    m["end_ep"] = m.market_end.astype("int64") // 10**9
    m["up_won"] = (m.outcome == "Up").astype(np.int8)
    t = pd.read_parquet(f"{rb.ARCH}/{prefix}_ticks.parquet",
                        columns=["condition_id", "t", "bu", "au", "bd", "ad", "sau", "sad"])
    t = t.merge(m[["condition_id", "start_ep", "end_ep", "up_won"]], on="condition_id")
    t = t.sort_values(["condition_id", "t"])
    g = t.groupby("condition_id")
    t["midu"] = (t.bu + t.au) / 2
    t["midd"] = (t.bd + t.ad) / 2
    t["au_next"] = g["au"].shift(-1); t["ad_next"] = g["ad"].shift(-1)
    t["midu_next"] = g["midu"].shift(-1); t["midd_next"] = g["midd"].shift(-1)
    t["nv"] = ((g["t"].shift(-1) - t["t"]).between(1, 3)).astype(float)
    for c in ["au_next", "ad_next", "midu_next", "midd_next"]:
        t[c] = t[c].fillna(0.0)
    t = t.sort_values("t", kind="stable")
    return {c: t[c].values for c in
            ["condition_id", "t", "bu", "au", "bd", "ad", "sau", "sad",
             "start_ep", "end_ep", "up_won",
             "au_next", "ad_next", "midu_next", "midd_next", "nv"]}


def main():
    print("$1000 OPTIMISED config (exp100, full window) across assets.")
    print("Out-of-sample = every asset except BTC. 12-seed drawdown sweep.\n")
    print(f"{'asset':6} {'fills':>6} {'win%':>6} {'pnl med':>9} {'pnl min':>9} "
          f"{'DD med':>7} {'DD max':>7} {'DD%cap':>7} {'survive all?':>13}")
    print("-" * 86)
    for name, symbol, prefix in ASSETS:
        kdir = kline_dir(symbol)
        if not glob.glob(f"{kdir}/*.zip"):
            print(f"{name:6} (no klines in {kdir} — skipped)")
            continue
        bin_data = load_binance_asset(kdir)
        if bin_data is None:
            print(f"{name:6} (kline load failed — skipped)")
            continue
        base, price, vol, obi = bin_data
        ticks = load_ticks_asset(prefix)
        pnls, dds, nfills, wins = [], [], [], []
        for sd in SEEDS:
            fills, _ = run({**CFG, "seed": sd}, base, price, vol, ticks, obi=obi)
            if not fills:
                continue
            pnls.append(sum(f[2] for f in fills)); dds.append(drawdown(fills))
            nfills.append(len(fills)); wins.append(np.mean([f[3] for f in fills]))
        if not pnls:
            print(f"{name:6} (no fills — kline/tick time overlap empty?)")
            continue
        pnls, dds = np.array(pnls), np.array(dds)
        survive = "YES" if dds.max() < CAPITAL else f"NO ({(dds>=CAPITAL).sum()}/{len(pnls)})"
        print(f"{name:6} {int(np.median(nfills)):>6} {np.mean(wins):>6.1%} "
              f"{np.median(pnls):>+9.0f} {pnls.min():>+9.0f} {np.median(dds):>7.0f} "
              f"{dds.max():>7.0f} {dds.max()/CAPITAL:>7.1%} {survive:>13}")
    print("-" * 86)
    print("Median/min over 12 seeds. 'survive all?' = maxDD < $1000 on every seed.")
    print("BTC = in-sample (tuned). Others = out-of-sample robustness of exp100.")


if __name__ == "__main__":
    main()
