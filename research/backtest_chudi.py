"""Backtest the chudi.dev "momentum breakout" strategy on REAL Binance data.

The blog (algorithmic-trading-python-ai-complete-guide) claims:
  * SIGNAL: a 2-sigma upside breakout on the last 20 five-minute BTC closes
    (z = (close - mean)/std > 2.0) marks momentum that continues, because
    prediction-market prices "lag price momentum by 30-90 seconds".
  * EXITS: scale out 50% at +2%, 50% at +5%, hard stop at -3%, time-stop at 4h.
  * CLAIM: +$0.38 EV per $100 at a 55% win rate.

Two falsifiable questions this answers from real candles (no Polymarket book
needed, because the *source* of any edge is the BTC move itself):

  1) PREDICTIVE EDGE — after the signal fires, does price actually continue
     up more often than the base rate? (directional hit rate + forward return,
     vs the unconditional next-candle base rate, with a binomial z-stat.)

  2) EXIT P&L — run the blog's exact scale-out/stop/time-stop on the real
     forward price path and report realised EV per $100, win rate, and how it
     compares to the post's +$0.38 claim — before and after a costs overlay.

Data: Binance 5m klines (public, no auth). Usage:
  python research/backtest_chudi.py [months]      (default 24)
"""
import json
import math
import statistics
import sys
import time
import urllib.request

HOSTS = ["https://api.binance.com", "https://data-api.binance.vision"]
SYMBOL = "BTCUSDT"
INTERVAL = "5m"
MS = 5 * 60 * 1000


def fetch_klines(months: int) -> list:
    want = int(months * 30 * 24 * 12)
    out: list = []
    cur_end = int(time.time() * 1000)
    host_i = 0
    while len(out) < want:
        url = (f"{HOSTS[host_i]}/api/v3/klines?symbol={SYMBOL}"
               f"&interval={INTERVAL}&limit=1000&endTime={cur_end}")
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        try:
            data = json.load(urllib.request.urlopen(req, timeout=25))
        except Exception as e:  # noqa: BLE001
            if host_i + 1 < len(HOSTS):
                host_i += 1
                print(f"  host fallback -> {HOSTS[host_i]} ({e})", file=sys.stderr)
                continue
            raise
        if not data:
            break
        out = data + out
        cur_end = data[0][0] - 1
        if len(out) % 20000 < 1000:
            print(f"  fetched {len(out):,} candles...", file=sys.stderr)
        time.sleep(0.15)
    closes = [float(k[4]) for k in out]
    highs = [float(k[2]) for k in out]
    lows = [float(k[3]) for k in out]
    opens_ms = [int(k[0]) for k in out]
    return closes, highs, lows, opens_ms


def zscore_signals(closes, win=20, thresh=2.0):
    """Replicate the blog's detect_momentum_breakout exactly: window is the
    last `win` closes INCLUDING the current one, population std, z of current."""
    sig = []  # list of (index, z) where a fired LONG signal closes
    for i in range(win - 1, len(closes)):
        w = closes[i - win + 1: i + 1]
        m = sum(w) / win
        var = sum((x - m) ** 2 for x in w) / win
        sd = var ** 0.5
        if sd == 0:
            continue
        z = (w[-1] - m) / sd
        if z > thresh:
            sig.append((i, z))
    return sig


def fwd_return(closes, i, n):
    j = i + n
    if j >= len(closes):
        return None
    return closes[j] / closes[i] - 1.0


def binom_z(wins, n, p0=0.5):
    if n == 0:
        return float("nan")
    p = wins / n
    se = (p0 * (1 - p0) / n) ** 0.5
    return (p - p0) / se if se else float("nan")


def simulate_exit(closes, highs, lows, i, tp1=0.02, tp2=0.05, stop=-0.03,
                  time_stop=48):
    """Blog's exit logic on the real forward path, entering long at close[i].
    50% scaled out at +2%, 50% at +5%, full stop at -3%, time-stop after
    `time_stop` candles (4h=48). Returns total return on a $1 notional
    (fraction). Intra-candle highs/lows decide target/stop touches; stop
    checked before target within a candle (conservative)."""
    entry = closes[i]
    tp1_px, tp2_px, stop_px = entry * (1 + tp1), entry * (1 + tp2), entry * (1 + stop)
    half_open = 1.0  # fraction of position still in the first (TP1) tranche
    pnl = 0.0
    tp1_done = False
    end = min(i + time_stop, len(closes) - 1)
    for j in range(i + 1, end + 1):
        # stop first (worst-case ordering): closes the WHOLE remaining position
        if lows[j] <= stop_px:
            rem = (0.5 if tp1_done else 1.0)
            pnl += rem * stop
            return pnl
        if not tp1_done and highs[j] >= tp1_px:
            pnl += 0.5 * tp1
            tp1_done = True
        if tp1_done and highs[j] >= tp2_px:
            pnl += 0.5 * tp2
            return pnl
    # time-stop: mark remaining position out at the final close
    final = closes[end] / entry - 1.0
    rem = 0.5 if tp1_done else 1.0
    pnl += rem * final
    return pnl


def fmt_pct(x):
    return f"{100 * x:+.3f}%"


def main():
    months = int(sys.argv[1]) if len(sys.argv) > 1 else 24
    print(f"Fetching ~{months} months of {SYMBOL} {INTERVAL} klines...", file=sys.stderr)
    closes, highs, lows, opens_ms = fetch_klines(months)
    n = len(closes)
    span_days = (opens_ms[-1] - opens_ms[0]) / 86400000
    print("=" * 72)
    print(f"DATA: {n:,} candles | {span_days:.0f} days "
          f"({span_days/365:.1f}y) | {SYMBOL} {INTERVAL}")
    print("=" * 72)

    # ---- base rate: unconditional next-candle up probability ----
    ups = sum(1 for k in range(n - 1) if closes[k + 1] > closes[k])
    base = ups / (n - 1)
    print(f"\nBASE RATE  P(next 5m candle up) unconditionally = {base:.4f} "
          f"({ups:,}/{n-1:,})")

    horizons = [("5m", 1), ("15m", 3), ("30m", 6), ("1h", 12)]
    for thresh in (2.0, 2.5, 3.0):
        sig = zscore_signals(closes, thresh=thresh)
        ns = len(sig)
        print(f"\n{'='*72}\nSIGNAL z>{thresh}  fired {ns:,} times "
              f"({ns/span_days:.1f}/day)\n{'='*72}")
        if ns == 0:
            continue
        print("PREDICTIVE EDGE (does the up-move continue?)")
        for label, h in horizons:
            rets = [fwd_return(closes, i, h) for i, _ in sig]
            rets = [r for r in rets if r is not None]
            if not rets:
                continue
            wins = sum(1 for r in rets if r > 0)
            hit = wins / len(rets)
            z = binom_z(wins, len(rets), base if h == 1 else 0.5)
            mean_r = statistics.mean(rets)
            med_r = statistics.median(rets)
            ref = "vs base" if h == 1 else "vs 50%"
            print(f"  +{label:3}: hit {hit:.3f} ({wins}/{len(rets)}) {ref} "
                  f"z={z:+.2f} | mean {fmt_pct(mean_r)} median {fmt_pct(med_r)}")

        # ---- blog exit simulation on the real path ----
        pnls = [simulate_exit(closes, highs, lows, i) for i, _ in sig
                if i + 1 < n]
        if pnls:
            wins = sum(1 for p in pnls if p > 0)
            avg = statistics.mean(pnls)
            tot = sum(pnls)
            gross_per100 = avg * 100
            # cost overlay: PM taker fee+spread realistically ~1.5% round trip
            # on the binary token; here applied as 0.3% per fill x ~2.5 fills
            cost = 0.0075
            net_per100 = (avg - cost) * 100
            print("BLOG EXIT P&L  (50% @+2%, 50% @+5%, stop -3%, 4h time-stop)")
            print(f"  trades {len(pnls)} | win rate {wins/len(pnls):.3f} | "
                  f"avg/trade {fmt_pct(avg)}")
            print(f"  EV/$100 gross {gross_per100:+.3f}  "
                  f"(blog claims +$0.38)  | net of ~0.75% costs {net_per100:+.3f}")


if __name__ == "__main__":
    main()
