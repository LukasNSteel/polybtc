"""Selection backtest over REALIZED settled 5m snipe fills.

Two levers, evaluated separately AND jointly (to expose overlap, not sum it):
  1. timing band  -> keep fills with close_buffer <= t_remaining <= max_t_rem
  2. trend filter -> drop fills that FADE momentum at fire time (BUY DN while BTC
     is running up, or BUY UP while running down), reconstructed from Binance 1m
     klines (proxy for the live 45s momentum the strategy uses).

It's a SELECTION backtest: we keep/drop trades we actually made and sum their
realized P&L. It can't model trades we never placed, and the momentum is a 1m-
granularity proxy — so treat magnitudes as directional, and watch the n per cell.
"""
import json
import urllib.request
from collections import defaultdict

# ---- 1. realized settled 5m fills from the shadow log + SETTLE lines ----
import re
from glob import glob

SETTLE = re.compile(r"SETTLE (.+?) -> (UP|DOWN) \| payout \$[0-9.]+ "
                    r"cost \$[0-9.]+ pnl \$([+-][0-9.]+)")
settles = {}
for path in glob("logs/session_*.log"):
    for line in open(path, errors="ignore"):
        m = SETTLE.search(line)
        if m:
            title, outc, pnl = m.groups()
            settles[title.strip()] = ("up" if outc == "UP" else "dn", float(pnl))

fills = []
for line in open("logs/shadow_taker.jsonl", errors="ignore"):
    try:
        r = json.loads(line)
    except Exception:
        continue
    if r.get("type") != "attempt" or not r.get("filled") or r.get("kind") != "5m":
        continue
    s = settles.get((r.get("title") or "").strip())
    if not s or r.get("t_remaining_s") is None or not r.get("ts"):
        continue
    outc, pnl = s
    fills.append({"ts": r["ts"], "trem": r["t_remaining_s"], "side": r["side"],
                  "pnl": pnl, "win": r["side"] == outc})
print(f"settled 5m fills: {len(fills)}")

# ---- 2. Binance 1m klines covering the span, momentum proxy ----
lo = int(min(f["ts"] for f in fills)) - 2400
hi = int(max(f["ts"] for f in fills)) + 120
kl = {}
t = lo
while t < hi:
    url = (f"https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1m"
           f"&startTime={t*1000}&endTime={min(hi, t+1000*60)*1000}&limit=1000")
    data = json.load(urllib.request.urlopen(url, timeout=30))
    if not data:
        break
    for k in data:
        kl[k[0] // 1000] = float(k[4])  # minute-open-sec -> close
    t = data[-1][0] // 1000 + 60
ks = sorted(kl)
print(f"klines: {len(ks)}")
import bisect
import math


def price_at(ts):
    i = bisect.bisect_right(ks, ts) - 1
    return kl[ks[i]] if i >= 0 else None


# 1m log-returns std over the whole span as the vol normaliser
rets = [math.log(kl[ks[i]] / kl[ks[i - 1]]) for i in range(1, len(ks))
        if kl[ks[i - 1]] > 0]
import statistics as st
vol1m = st.pstdev(rets) or 1e-9

LOOKBACK = 120  # seconds


def momentum_z(ts):
    p_now, p_then = price_at(ts), price_at(ts - LOOKBACK)
    if not p_now or not p_then:
        return 0.0
    # ret over LOOKBACK normalised to a 1m-vol z (LOOKBACK/60 minutes of drift)
    return math.log(p_now / p_then) / (vol1m * math.sqrt(LOOKBACK / 60))


for f in fills:
    z = momentum_z(f["ts"])
    f["z"] = z
    # fade = betting against the run
    f["fade"] = (f["side"] == "dn" and z > 0) or (f["side"] == "up" and z < 0)
    f["fade_mag"] = abs(z)


def evaluate(rows, label):
    if not rows:
        print(f"  {label:34} n=0")
        return
    n = len(rows)
    w = sum(1 for r in rows if r["win"])
    pnl = sum(r["pnl"] for r in rows)
    print(f"  {label:34} n={n:>3}  W{w}/{n-w}  win {w/n*100:>3.0f}%  "
          f"net {pnl:>+8.2f}  avg {pnl/n:>+6.2f}")


def keep_band(f, cb, mx):
    return cb <= f["trem"] and (mx == 0 or f["trem"] <= mx)


print(f"\nBASELINE (all settled 5m fills):")
evaluate(fills, "no gate")

print("\n=== TIMING BAND sweep (close_buffer .. max_t_rem) ===")
for cb in (20, 30, 45):
    for mx in (0, 150, 120, 90, 60):
        band = [f for f in fills if keep_band(f, cb, mx)]
        evaluate(band, f"buffer {cb}s, max_t_rem {mx or 'full'}")

print("\n=== TREND FILTER alone (drop fades above z-threshold) ===")
for k in (0.0, 0.5, 1.0):
    kept = [f for f in fills if not (f["fade"] and f["fade_mag"] >= k)]
    evaluate(kept, f"drop fades z>={k}")

print("\n=== JOINT: best timing band [30,60]s  x  trend filter ===")
band = [f for f in fills if keep_band(f, 30, 60)]
evaluate(band, "timing [30,60]s only")
for k in (0.0, 0.5, 1.0):
    kept = [f for f in band if not (f["fade"] and f["fade_mag"] >= k)]
    evaluate(kept, f"  + drop fades z>={k}")

print("\n=== reference: side cut (up-only) x timing ===")
evaluate([f for f in fills if f["side"] == "up"], "up-only, full window")
evaluate([f for f in fills if f["side"] == "up" and keep_band(f, 30, 60)],
         "up-only, [30,60]s")

# ---- TARGETED: widening the LIVE 5m window [30,90] -> [30,150] ----
# These are REALIZED fills (filled live, ground-truth settled), so "filled" here
# is historical fact. Going forward the live FAK race fills only ~42% of the
# attempts in a band, so multiply the win COUNTS below by ~0.42 for a forward
# estimate; win RATE and avg-pnl/fill carry over.
print("\n" + "=" * 60)
print("WIDEN LIVE WINDOW: [30,90]s (current) -> [30,150]s (proposed)")
print("=" * 60)
cur = [f for f in fills if keep_band(f, 30, 90)]
prop = [f for f in fills if keep_band(f, 30, 150)]
incr = [f for f in fills if 90 < f["trem"] <= 150]  # the NEW slice we'd recapture
evaluate(cur, "current  [30, 90]s")
evaluate(prop, "proposed [30,150]s")
evaluate(incr, "INCREMENTAL (90,150]s only")
print("\n  incremental slice with the live trend filter applied:")
for k in (0.5, 1.0, 1.5):
    kept = [f for f in incr if not (f["fade"] and f["fade_mag"] >= k)]
    dropped = len(incr) - len(kept)
    evaluate(kept, f"  (90,150] minus fades z>={k} (-{dropped})")
if incr:
    print("\n  per-trade in the (90,150]s recapture slice:")
    print(f"    {'side':>4} {'t_rem':>6} {'z(120s)':>8} {'fade':>5} "
          f"{'win':>4} {'pnl':>8}")
    for f in sorted(incr, key=lambda r: r["trem"]):
        print(f"    {f['side']:>4} {f['trem']:6.1f} {f['z']:8.2f} "
              f"{('yes' if f['fade'] else 'no'):>5} "
              f"{('W' if f['win'] else 'L'):>4} {f['pnl']:>+8.2f}")
