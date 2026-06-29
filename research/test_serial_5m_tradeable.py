"""Is the 5m mean-reversion edge ACTUALLY tradeable, or does the book already price it?

test_serial_5m.py showed BTC 5m candles mean-revert: after a big UP move the next
candle is ~55% likely DOWN (significant). But a fresh market opens at fair~0.50.
The question that decides whether there's money in it:

  After a big prior move, where does the POLYMARKET BOOK actually open the next
  market? If the book already skews toward the reversion (prices DOWN > 0.50
  after a big up move), the edge is gone. If it opens ~0.50, we can take it.

Join: Binance prior-candle move (by candle epoch in the slug) -> the live
calibration tape's EARLY book p_up for the next market + its settled outcome.
Then the tradeable test: after a big prior UP move, BUY the next market's DOWN
side at the book's early price, hold to settle, net the real p(1-p) fee.

Run: python research/test_serial_5m_tradeable.py
"""
import io
import ssl
import time
import urllib.request

import numpy as np
import pandas as pd

CACHE = "research/data/btc_5m_240d.csv"
CAL = "research/data/calibration_live.csv"
fee = lambda p: 0.07 * p * (1 - p)  # noqa: E731


def candles():
    try:
        return pd.read_csv(CACHE)
    except FileNotFoundError:
        pass
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    end = int(time.time() * 1000)
    cur = end - 240 * 86400 * 1000
    out = []
    while cur < end:
        q = (f"https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=5m"
             f"&startTime={cur}&limit=1000")
        with urllib.request.urlopen(q, timeout=20, context=ctx) as r:
            k = pd.read_json(io.BytesIO(r.read()))
        if not len(k):
            break
        out.append(k)
        nxt = int(k.iloc[-1, 0]) + 1
        if nxt <= cur or len(k) < 1000:
            cur = nxt
            break
        cur = nxt
    k = pd.concat(out, ignore_index=True)
    df = pd.DataFrame({"ts": (k[0] // 1000).astype(int),
                       "open": k[1].astype(float), "close": k[4].astype(float)})
    df = df.drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
    df["bps"] = (df.close - df.open) / df.open * 1e4
    df.to_csv(CACHE, index=False)
    return df


def main():
    c = candles()[["ts", "bps"]].rename(columns={"bps": "prior_bps"})
    # prior candle move keyed to the NEXT candle's open epoch
    c["next_open"] = c.ts + 300

    cal = pd.read_csv(CAL)
    cal = cal[cal.kind == "5m"].dropna(subset=["p_up"]).copy()
    cal["epoch"] = cal.slug.str.extract(r"-(\d+)$").astype(int)

    # per market: the EARLY (near-open) book p_up, and the settled outcome
    rows = []
    for slug, g in cal.groupby("slug"):
        g = g.sort_values("t_remaining")  # ascending tau; we want the largest tau
        epoch = int(g.epoch.iloc[0])
        open_row = g.loc[g.t_remaining.idxmax()]
        if open_row.t_remaining < 180:   # need a genuine near-open quote
            continue
        oc = g.outcome.dropna()
        last = g.loc[g.t_remaining.idxmin(), "p_up"]
        y = float(oc.iloc[-1]) if len(oc) else (1.0 if last >= 0.98 else 0.0 if last <= 0.02 else np.nan)
        rows.append((epoch, open_row.p_up, open_row.t_remaining, y))
    m = pd.DataFrame(rows, columns=["epoch", "open_p_up", "open_tau", "y"]).dropna(subset=["y"])
    m = m.merge(c[["next_open", "prior_bps"]], left_on="epoch", right_on="next_open", how="inner")
    print(f"joined {len(m)} consecutive 5m market pairs "
          f"(book open quote @ mean tau {m.open_tau.mean():.0f}s)\n")

    edges = [-1e9, -40, -20, -10, -3, 3, 10, 20, 40, 1e9]
    labels = ["<-40", "-40..-20", "-20..-10", "-10..-3", "-3..3",
              "3..10", "10..20", "20..40", ">40"]
    m["bin"] = pd.cut(m.prior_bps, edges, labels=labels)

    print("prior move ->  book's OPEN P(up)   realized P(up)   gap    fade-DOWN EV(net fee)")
    print("-" * 84)
    for lab in labels:
        b = m[m.bin == lab]
        n = len(b)
        if n < 30:
            print(f"{lab:>10} {n:>5}  (too few)")
            continue
        book = b.open_p_up.mean()
        real = b.y.mean()
        # fade: buy DOWN at the book's down price (1-open_p_up), pnl = (1-y) - (1-p) - fee
        pdn = 1 - b.open_p_up
        pnl_dn = (1 - b.y) - pdn - fee(pdn)
        # buy UP at book up price
        pnl_up = b.y - b.open_p_up - fee(b.open_p_up)
        best = "DOWN" if pnl_dn.mean() >= pnl_up.mean() else "UP"
        ev = 100 * max(pnl_dn.mean(), pnl_up.mean())
        se = 100 * (pnl_dn if best == "DOWN" else pnl_up).std() / np.sqrt(n)
        print(f"{lab:>10} {n:>5}     {book:6.3f}          {real:6.3f}     "
              f"{real-book:+.3f}   bet {best} {ev:+6.2f}c ± {1.96*se:.2f}")
    print("\nKEY: if 'book open P(up)' already moves OPPOSITE the prior move (lower")
    print("after a big up move), the MMs price the reversion and the edge is gone.")
    print("If book stays ~0.50 while realized P(up) skews, the fade EV is real.")


if __name__ == "__main__":
    main()
