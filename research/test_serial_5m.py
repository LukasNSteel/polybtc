"""Serial correlation of consecutive 5-minute BTC candles.

Question: if the PREVIOUS 5m market was UP by x, what's P(next market is DOWN)?

A Polymarket 5m "Up/Down" resolves on close-vs-open of one 5m candle, and
consecutive markets are back-to-back candles (prev close == next open). So this
is exactly the autocorrelation of consecutive non-overlapping 5m log returns,
conditioned on the size of the prior move. We measure:

  * P(next up) and P(next down) conditioned on the PRIOR candle's signed move,
    bucketed by size, with a binomial test vs the 50% base rate;
  * sign- and value-autocorrelation at lag 1;
  * the TRADEABLE EV: at a fresh candle's open, fair P(up) ~ 0.50 (spot == open),
    and the taker fee peaks at ~1.75c/share at 0.50. So an edge is only tradeable
    if the conditional probability clears 0.50 by more than the fee
    (need P > ~0.5175 to bet, either side).

Pulls a long 5m history from Binance (fallback: local 1m klines -> 5m).
Run: python research/test_serial_5m.py
"""
import gzip
import io
import sys
import time

import numpy as np
import pandas as pd

FEE_AT_HALF = 0.07 * 0.5 * 0.5  # 0.0175 /share, the worst-case (mid) taker fee


def fetch_binance(symbol="BTCUSDT", interval="5m", days=240):
    import ssl
    import urllib.request
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # public klines; tolerate sandbox SSL proxy
    end = int(time.time() * 1000)
    start = end - days * 86400 * 1000
    out = []
    cur = start
    url = "https://api.binance.com/api/v3/klines"
    while cur < end:
        q = f"{url}?symbol={symbol}&interval={interval}&startTime={cur}&limit=1000"
        with urllib.request.urlopen(q, timeout=20, context=ctx) as r:
            data = pd.read_json(io.BytesIO(r.read()))
        if not len(data):
            break
        out.append(data)
        last = int(data.iloc[-1, 0])
        if last <= cur:
            break
        cur = last + 1
        if len(data) < 1000:
            break
    k = pd.concat(out, ignore_index=True)
    df = pd.DataFrame({"ts": (k[0] // 1000).astype(int),
                       "open": k[1].astype(float), "close": k[4].astype(float)})
    return df.drop_duplicates("ts").sort_values("ts").reset_index(drop=True)


def from_local_1m(path="research/data/binance_1m.csv.gz"):
    with gzip.open(path, "rt") as f:
        m = pd.read_csv(f)
    m["bucket"] = (m.ts // 300) * 300
    g = m.groupby("bucket")
    df = pd.DataFrame({"ts": g.ts.first().index,
                       "open": g.open.first().values,
                       "close": g.close.last().values})
    # require full 5-bar candles only
    counts = g.size().values
    return df[counts >= 4].reset_index(drop=True)


def main():
    try:
        df = fetch_binance(days=240)
        src = f"Binance API 5m, {len(df)} candles"
    except Exception as e:  # noqa: BLE001
        print(f"(Binance fetch failed: {e}; falling back to local 1m)")
        df = from_local_1m()
        src = f"local 1m->5m, {len(df)} candles"

    df["ret"] = (df.close - df.open) / df.open          # candle return
    df["bps"] = df.ret * 1e4
    df["up"] = (df.close >= df.open).astype(int)        # ties -> up (Polymarket rule)
    df["next_up"] = df.up.shift(-1)
    df["next_ret"] = df.bps.shift(-1)
    # only chain candles that are actually back-to-back (next open == this close)
    contiguous = (df.ts.shift(-1) - df.ts).abs() <= 360
    d = df[contiguous].dropna(subset=["next_up"]).copy()
    d["next_up"] = d["next_up"].astype(int)

    span_h = (df.ts.max() - df.ts.min()) / 3600
    print(f"source: {src}  ({span_h/24:.0f} days)")
    print(f"base rate P(up) = {df.up.mean():.4f}  (n={len(df)})")
    print(f"lag-1 sign autocorr = {np.corrcoef(d.up, d.next_up)[0,1]:+.4f}   "
          f"return autocorr = {np.corrcoef(d.bps, d.next_ret)[0,1]:+.4f}\n")

    # ---- conditional table by PRIOR signed move size ----
    edges = [-1e9, -40, -20, -10, -3, 3, 10, 20, 40, 1e9]
    labels = ["<-40", "-40..-20", "-20..-10", "-10..-3", "-3..3",
              "3..10", "10..20", "20..40", ">40"]
    d["bin"] = pd.cut(d.bps, edges, labels=labels)
    print("PRIOR 5m move (bps)  ->  what the NEXT candle does")
    print(f"{'prior move':>12} {'n':>6} {'P(next up)':>11} {'P(next dn)':>11} "
          f"{'vs 50%':>8} {'best bet EV':>16}")
    print("-" * 70)
    for lab in labels:
        b = d[d.bin == lab]
        n = len(b)
        if n < 50:
            print(f"{lab:>12} {n:>6}  (too few)")
            continue
        p_up = b.next_up.mean()
        p_dn = 1 - p_up
        # binomial 2-sided z vs 0.5
        z = (p_up - 0.5) / np.sqrt(0.25 / n)
        sig = "*" if abs(z) > 1.96 else " "
        # best tradeable bet at open (fair ~0.5): bet the higher-prob side, net fee
        p_best = max(p_up, p_dn)
        ev = 100 * (p_best - 0.5 - FEE_AT_HALF)
        side = "UP" if p_up >= p_dn else "DOWN"
        print(f"{lab:>12} {n:>6}   {p_up:>8.3f}    {p_dn:>8.3f}   "
              f"{z:>+6.1f}{sig} bet {side} {ev:>+6.2f}c")

    print(f"\nTradeable threshold: |P-0.5| must exceed the {100*FEE_AT_HALF:.2f}c "
          f"open-price fee, i.e. P>{0.5+FEE_AT_HALF:.4f}, to beat just holding cash.")
    print("EV>0 above means a bet at a fair (0.50) open would have been +EV in-sample.")


if __name__ == "__main__":
    main()
