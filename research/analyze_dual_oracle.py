"""Binance vs Kraken as the BTC signal oracle for a Dublin-hosted live bot.

The decision has two independent pieces:

  1. INFORMATION LEAD (Λ): does Binance's tape move before Kraken's, and by how
     many ms? A property of the venues, independent of where we host. Measured
     from cross-correlation of mid-return series on the EXCHANGE clock, primarily
     BOOK-vs-BOOK (Binance diff-depth `bbo_exch` vs Kraken book), with
     trade-vs-book and recv-clock comparisons as cross-checks. We scan several
     grid resolutions because Kraken's tape is sparse at 25 ms.

  2. DELIVERY LATENCY to Dublin: Binance matches in Tokyo (~105 ms one-way to
     Dublin); Kraken matches in London (~6 ms to Dublin). Modeled from published
     AWS inter-region / Kraken-colo figures, NOT from this machine.

A Dublin bot reading a venue at wall-clock T sees the true market as of:
    Binance:  T - D_binance                 (Binance ≈ price-discovery leader)
    Kraken:   T - D_kraken - Λ              (Kraken's print already lags by Λ)
=> Binance is the fresher signal iff  Λ > (D_binance - D_kraken) ≈ 99 ms.

Usage: python research/analyze_dual_oracle.py [research/data/dual_oracle_*.csv]
"""

import glob
import sys

import numpy as np
import pandas as pd

# modeled one-way delivery latency to a Dublin (eu-west-1) VPS, milliseconds.
# Binance engine = AWS Tokyo (Tokyo<->Ireland RTT ~202ms); Kraken = Equinix
# London (London<->Dublin RTT ~10ms) + Cloudflare edge.
D_BINANCE_MS = 105.0
D_KRAKEN_MS = 6.0
MAX_LAG_MS = 600               # real spot lead-lag is well within this; caps noise
RESOLUTIONS = [50, 100, 200]   # grid sizes (ms) scanned for the lead-lag peak

path = sys.argv[1] if len(sys.argv) > 1 else sorted(glob.glob("research/data/dual_oracle_*.csv"))[-1]
df = pd.read_csv(path)
df = df[~df.src.astype(str).str.startswith("#")]
print(f"=== {path} ===")
span = df.recv_ts.max() - df.recv_ts.min()
print(f"{len(df)} rows over {span/60:.1f} min")
for (src, kind), g in df.groupby(["src", "kind"]):
    print(f"  {src:8} {kind:9} n={len(g):6d}")


def mid_series(sub, clock, grid_ms):
    """Forward-filled mid on a uniform ms grid snapped to GLOBAL grid_ms
    boundaries so two series share index values and can be joined."""
    sub = sub.dropna(subset=[clock]).sort_values(clock)
    if sub.empty:
        return pd.Series(dtype=float)
    t = sub[clock].values * 1000.0
    p = sub.price.values
    start = (int(t[0]) // grid_ms) * grid_ms
    grid = np.arange(start, t[-1] + grid_ms, grid_ms)
    idx = np.clip(np.searchsorted(t, grid, side="right") - 1, 0, len(p) - 1)
    return pd.Series(p[idx], index=grid.astype(np.int64))


def xcorr_peak(b_mid, k_mid, grid_ms):
    """Return (peak_lag_ms, peak_corr, corr_at_0, n). Positive lag => Kraken
    follows Binance (Binance leads)."""
    join = pd.concat({"b": b_mid, "k": k_mid}, axis=1).dropna()
    if len(join) < 50:
        return None
    rb = np.diff(np.log(join.b.values))
    rk = np.diff(np.log(join.k.values))
    rb = rb - rb.mean(); rk = rk - rk.mean()
    denom = np.sqrt((rb @ rb) * (rk @ rk))
    if denom == 0:
        return None
    maxlag = MAX_LAG_MS // grid_ms
    lags = np.arange(-maxlag, maxlag + 1)
    corrs = np.empty(len(lags))
    for i, L in enumerate(lags):
        if L >= 0:
            a, b = rb[L:], rk[:len(rk) - L] if L else rk
        else:
            a, b = rb[:len(rb) + L], rk[-L:]
        n = min(len(a), len(b))
        corrs[i] = (a[:n] @ b[:n]) / denom if n else np.nan
    j = int(np.nanargmax(corrs))
    c0 = corrs[np.where(lags == 0)][0]
    return int(lags[j] * grid_ms), float(corrs[j]), float(c0), len(join)


def scan(b_sub, b_clock, k_sub, k_clock, label):
    """Run the lead-lag scan across resolutions; return the best (highest-corr)
    peak estimate."""
    print(f"\n  [{label}]")
    best = None
    for g in RESOLUTIONS:
        r = xcorr_peak(mid_series(b_sub, b_clock, g),
                       mid_series(k_sub, k_clock, g), g)
        if r is None:
            print(f"     grid {g:>3}ms: insufficient/degenerate")
            continue
        lag, c, c0, n = r
        bound = "  (!boundary)" if abs(lag) >= MAX_LAG_MS else ""
        print(f"     grid {g:>3}ms: Λ={lag:+5d}ms  corr={c:.3f}  corr@0={c0:.3f}  "
              f"bins={n}{bound}")
        if best is None or c > best[1]:
            best = (lag, c)
    return best


bbo_exch_b = df[(df.src == "binance") & (df.kind == "bbo_exch")]
bbo_b = df[(df.src == "binance") & (df.kind == "bbo")]
trade_b = df[(df.src == "binance") & (df.kind == "trade")]
bbo_k = df[(df.src == "kraken") & (df.kind == "bbo")]

def corr_curve(b_sub, b_clock, k_sub, k_clock, grid_ms=100, span=500):
    """Print corr at each lag in [-span, span] so the peak SHAPE is visible."""
    b_mid, k_mid = mid_series(b_sub, b_clock, grid_ms), mid_series(k_sub, k_clock, grid_ms)
    join = pd.concat({"b": b_mid, "k": k_mid}, axis=1).dropna()
    if len(join) < 50:
        print("     (insufficient)")
        return
    rb = np.diff(np.log(join.b.values)); rk = np.diff(np.log(join.k.values))
    rb -= rb.mean(); rk -= rk.mean()
    denom = np.sqrt((rb @ rb) * (rk @ rk))
    print(f"     lag(ms): corr   [grid {grid_ms}ms, {len(join)} bins, + = Binance leads]")
    line = []
    for lag in range(-span, span + 1, grid_ms):
        L = lag // grid_ms
        if L >= 0:
            a, b = rb[L:], rk[:len(rk) - L] if L else rk
        else:
            a, b = rb[:len(rb) + L], rk[-L:]
        n = min(len(a), len(b))
        c = (a[:n] @ b[:n]) / denom if n and denom else float("nan")
        line.append(f"{lag:+4d}:{c:.3f}")
    print("     " + "  ".join(line))


print("\n--- information lead-lag (Λ)   [+ = Binance leads, ms] ---")
primary = scan(bbo_exch_b, "exch_ts", bbo_k, "exch_ts",
               "PRIMARY  book-vs-book, exchange clock (Bnc depth E vs Krk book)")
print("\n  primary correlation curve:")
corr_curve(bbo_exch_b, "exch_ts", bbo_k, "exch_ts")
scan(trade_b, "exch_ts", bbo_k, "exch_ts",
     "cross-check  trade-vs-book, exchange clock")
recv = scan(bbo_b, "recv_ts", bbo_k, "recv_ts",
            "cross-check  book-vs-book, RECV clock (contains AU path skew)")

# apparent one-way from this host (sanity only; contaminated by clock offset)
Lb = np.median(trade_b.eval("recv_ts - exch_ts")) * 1000
Lk = np.median(bbo_k.eval("recv_ts - exch_ts")) * 1000
print(f"\n  apparent one-way from THIS host (incl ~machine clock offset): "
      f"binance≈{Lb:.0f}ms kraken≈{Lk:.0f}ms (this host is in AU, not Dublin)")

# split-half stability on the primary book-vs-book estimate
print("\n  split-half stability (primary book-vs-book):")
tsplit = bbo_exch_b.exch_ts.quantile(0.5)
for nm, lo, hi in [("first half", -np.inf, tsplit), ("second half", tsplit, np.inf)]:
    r = xcorr_peak(mid_series(bbo_exch_b[(bbo_exch_b.exch_ts > lo) & (bbo_exch_b.exch_ts <= hi)], "exch_ts", 100),
                   mid_series(bbo_k[(bbo_k.exch_ts > lo) & (bbo_k.exch_ts <= hi)], "exch_ts", 100), 100)
    print(f"     {nm}: " + (f"Λ={r[0]:+d}ms corr={r[1]:.3f}" if r else "insufficient"))

LAM = float(primary[0]) if primary else 0.0
print(f"\n  >>> adopted information lead Λ ≈ {LAM:+.0f} ms "
      f"(from highest-correlation book-vs-book estimate)")

# realized short-horizon volatility -> bps cost of staleness
true_mid = mid_series(bbo_b, "recv_ts", 25)
lp = true_mid.values


def drift_bps(horizon_ms):
    steps = max(1, int(round(horizon_ms / 25)))
    if steps >= len(lp):
        return float("nan")
    r = np.log(lp[steps:] / lp[:-steps])
    return np.sqrt(np.mean(r ** 2)) * 1e4


binance_stale = D_BINANCE_MS
kraken_stale = D_KRAKEN_MS + max(LAM, 0.0)
print("\n--- staleness cost in Dublin (bps of BTC mispricing) ---")
print(f"  Binance signal age: {binance_stale:5.0f} ms -> {drift_bps(binance_stale):.2f} bps")
print(f"  Kraken  signal age: {kraken_stale:5.0f} ms "
      f"(= {D_KRAKEN_MS:.0f} delivery + {max(LAM,0):.0f} info-lag) "
      f"-> {drift_bps(kraken_stale):.2f} bps")

print("\n--- VERDICT ---")
threshold = D_BINANCE_MS - D_KRAKEN_MS
print(f"  Binance wins on freshness iff Λ > (D_binance - D_kraken) = {threshold:.0f} ms")
if LAM > threshold:
    edge = drift_bps(kraken_stale) - drift_bps(binance_stale)
    print(f"  Λ ≈ {LAM:.0f}ms > {threshold:.0f}ms  =>  BINANCE is fresher in Dublin "
          f"(~{edge:+.2f} bps vs Kraken).")
else:
    edge = drift_bps(binance_stale) - drift_bps(kraken_stale)
    print(f"  Λ ≈ {LAM:.0f}ms <= {threshold:.0f}ms  =>  KRAKEN (local) is fresher in "
          f"Dublin (~{edge:+.2f} bps vs Binance).")
print("\n  CAVEATS: (1) Polymarket BTC up/down settles on Chainlink/Binance, so "
      "Kraken adds basis/tracking error independent of latency. (2) Λ is bounded "
      "by Binance depth's 100ms batching + inter-venue clock skew (~few ms).")
