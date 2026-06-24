"""Apply the distance-to-strike gate to OUR ACTUAL live fills.

Reconstructs distance-to-strike for every real filled snipe in
logs/shadow_taker.jsonl, using the same `d = log(spot/open)/(vol*sqrt(t_rem))`
the live model computes, then recomputes our realized P&L with/without a sigma
gate. Tiny sample (n~25) — this is a sanity check, not a significant backtest.

Inputs:
  /tmp/shadow_taker.jsonl  — filled attempts (ts, slug=open epoch, t_rem, side,
                             avg_fill_px, filled_shares, kind)
  Binance 1s klines        — fetched via REST for the trade dates (cached)

Outcome is taken from Binance (close@close_ts vs open@open_ts). On the gated
(high-distance) subset Binance and Polymarket's resolver agree by construction;
disagreement (basis risk) only affects the near-the-money trades the gate drops.
"""
import json
import os
import ssl
import time
import urllib.request

import numpy as np

# local cert chain has a self-signed proxy cert; this is a read-only public
# Binance data fetch, so an unverified context is fine here.
_SSL = ssl._create_unverified_context()

CACHE = "/tmp/btc_1s_live.npz"
FEE = 0.07
MIN_VOL = 2e-5
DUR = {"5m": 300, "15m": 900, "1h": 3600, "4h": 14400}


def load_fills(path="/tmp/shadow_taker.jsonl"):
    fills = []
    for line in open(path):
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("type") == "attempt" and r.get("filled") and r.get("leg") == "snipe":
            fills.append(r)
    return fills


def fetch_klines(start_s, end_s):
    if os.path.exists(CACHE):
        z = np.load(CACHE)
        if z["sec"][0] <= start_s and z["sec"][-1] >= end_s:
            return z["sec"], z["close"]
    secs, closes = [], []
    t = start_s * 1000
    end_ms = end_s * 1000
    while t < end_ms:
        url = ("https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1s"
               f"&startTime={t}&endTime={end_ms}&limit=1000")
        with urllib.request.urlopen(url, timeout=30, context=_SSL) as resp:
            rows = json.load(resp)
        if not rows:
            break
        for r in rows:
            secs.append(r[0] // 1000)
            closes.append(float(r[4]))
        t = rows[-1][0] + 1000
        time.sleep(0.05)
    sec = np.array(secs, dtype=np.int64)
    close = np.array(closes, dtype=float)
    np.savez(CACHE, sec=sec, close=close)
    return sec, close


def build_grid(sec, close):
    base = sec[0]
    n = sec[-1] - base + 1
    price = np.full(n, np.nan)
    price[sec - base] = close
    idx = np.maximum.accumulate(np.where(np.isnan(price), 0, np.arange(n)))
    price = price[idx]
    # vol: EWMA of r^2, fast (hl 60s) / slow (hl 600s), take max, floor (matches
    # research/replay_binance.load_binance)
    r = np.zeros(n)
    r[1:] = np.log(price[1:] / price[:-1])
    samp = r * r
    af, as_ = 1 - 0.5 ** (1 / 60), 1 - 0.5 ** (1 / 600)
    vf = np.empty(n); vs = np.empty(n)
    cf = cs = samp[0]
    for i in range(n):
        cf += af * (samp[i] - cf)
        cs += as_ * (samp[i] - cs)
        vf[i] = cf; vs[i] = cs
    vol = np.maximum(np.sqrt(np.maximum(vf, vs)), MIN_VOL)
    return base, price, vol


def main():
    fills = load_fills()
    open_ts = [int(f["slug"].split("-")[-1]) for f in fills]
    span_lo = min(open_ts) - 1800
    span_hi = int(max(f["ts"] for f in fills)) + max(DUR.values()) // 2 + 60
    print(f"{len(fills)} filled snipes | fetching binance 1s "
          f"{span_lo}..{span_hi} ({(span_hi-span_lo)/3600:.1f}h)...", flush=True)
    sec, close = fetch_klines(span_lo, span_hi)
    base, price, vol = build_grid(sec, close)

    def at(ts):
        return price[min(max(int(ts) - base, 0), len(price) - 1)]

    rows = []
    for f, ots in zip(fills, open_ts):
        dur = DUR.get(f["kind"], 300)
        cts = ots + dur
        ts = f["ts"]
        spot = at(ts)
        openp = at(ots)
        v = vol[min(max(int(ts) - base, 0), len(vol) - 1)]
        t_rem = max(f.get("t_remaining_s") or (cts - ts), 1.0)
        d = np.log(spot / openp) / (v * np.sqrt(t_rem))
        fav_sigma = d if f["side"] == "up" else -d
        fav_usd = (spot - openp) if f["side"] == "up" else (openp - spot)
        up_won = at(cts) >= openp
        won = up_won if f["side"] == "up" else (not up_won)
        px = f["avg_fill_px"]; sh = f["filled_shares"]
        pnl = sh * ((1.0 if won else 0.0) - px) - FEE * px * (1 - px) * sh
        ret = (( 1.0 if won else 0.0) - px) / px - FEE * (1 - px)
        rows.append(dict(win=f["window"] if "window" in f else f["title"].split(" - ")[-1],
                         slug=f["slug"], side=f["side"], px=px, usd=round(sh * px, 2),
                         fav_sigma=round(float(fav_sigma), 2),
                         fav_usd=round(float(fav_usd), 1),
                         won=bool(won), pnl=round(float(pnl), 2), ret=float(ret)))

    print(f"\n{'window':28} {'side':>4} {'px':>5} {'$':>6} {'favσ':>6} "
          f"{'fav$':>7} {'won':>4} {'pnl':>8}")
    for r in rows:
        print(f"{r['win']:28} {r['side']:>4} {r['px']:>5.2f} {r['usd']:>6.2f} "
              f"{r['fav_sigma']:>6.2f} {r['fav_usd']:>7.1f} "
              f"{'Y' if r['won'] else 'N':>4} {r['pnl']:>+8.2f}")

    def summary(label, keep):
        sel = [r for r in rows if keep(r)]
        if not sel:
            print(f"{label:34} no trades")
            return
        pnl = sum(r["pnl"] for r in sel)
        w = sum(r["won"] for r in sel)
        dep = sum(r["usd"] for r in sel)
        print(f"{label:34} n={len(sel):>2} win={w}/{len(sel)} ({w/len(sel):.0%}) "
              f"dep=${dep:>6.0f} pnl=${pnl:>+7.2f} ROI={pnl/dep:>+6.1%}")

    print("\n" + "=" * 78)
    print("OUR LIVE FILLS — with vs without the distance gate (real $ P&L)")
    print("=" * 78)
    summary("ALL fills (no gate)", lambda r: True)
    summary("gate: favourable sigma >= 0.5", lambda r: r["fav_sigma"] >= 0.5)
    summary("gate: favourable sigma >= 1.0", lambda r: r["fav_sigma"] >= 1.0)
    summary("gate: favourable sigma >= 1.5", lambda r: r["fav_sigma"] >= 1.5)
    summary("gate: favourable $ >= 25", lambda r: r["fav_usd"] >= 25)
    summary("gate: favourable $ >= 50", lambda r: r["fav_usd"] >= 50)

    # ---- THE HYPOTHESIS TEST: same trades, correctly sized for live165 ----
    # flat $5/snipe, one fill per (market, side) (the inventory gate blocks the
    # 2nd same-side fill). P&L scales linearly with stake, so re-sizing just
    # re-weights each real outcome.
    print("\n" + "=" * 78)
    print("WAS IT THE SIZING? — replay our REAL outcomes at live165 sizing")
    print("=" * 78)
    seen = set()
    deduped = []
    for r in rows:
        key = (r["slug"], r["side"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(r)
    STAKE = 5.0
    net5 = sum(STAKE * r["ret"] for r in deduped)
    w5 = sum(r["won"] for r in deduped)
    dep5 = STAKE * len(deduped)
    print(f"actual (as-traded, oversized + stacked):   n={len(rows)} "
          f"win={sum(r['won'] for r in rows)}/{len(rows)} pnl=${sum(r['pnl'] for r in rows):+.2f}")
    print(f"live165 sizing (flat $5, 1 per mkt-side):   n={len(deduped)} "
          f"win={w5}/{len(deduped)} ({w5/len(deduped):.0%}) dep=${dep5:.0f} "
          f"pnl=${net5:+.2f}  ROI={net5/dep5:+.1%}")

    # ---- is the EDGE itself +/-? win rate vs the break-even at our prices ----
    avg_px = np.mean([r["px"] for r in deduped])
    q = w5 / len(deduped)
    be = avg_px * (1 + FEE * (1 - avg_px))   # win rate needed to break even at avg ask
    # 95% CI on the win rate (normal approx)
    se = np.sqrt(q * (1 - q) / len(deduped))
    print(f"\nedge check: avg entry ask {avg_px:.2f}  ->  break-even win rate "
          f"{be:.1%}")
    print(f"            our win rate {q:.1%}  (95% CI {q-1.96*se:.0%}..{q+1.96*se:.0%}, n={len(deduped)})")
    print(f"            verdict: {'BELOW' if q < be else 'ABOVE'} break-even, but CI "
          f"{'spans' if (q-1.96*se) < be < (q+1.96*se) else 'excludes'} it -> "
          f"{'too few trades to call the edge' if (q-1.96*se) < be < (q+1.96*se) else 'edge sign is significant'}")


if __name__ == "__main__":
    main()
