"""24h snipe scorecard + counterfactual re-score under trend_filter_sigma=1.0.

Joins:
  * shadow_taker.jsonl filled snipe attempts -> side, shares, fill px, trend_z,
    dist_sigma, t_remaining_s, ts  (the per-fill features incl. momentum)
  * session log settlement lines  "...<window> -> UP/DOWN | payout .. pnl .."
    -> ground-truth outcome per market window

Then reports realized W/L/PnL, and what the book becomes if we DROP every fill
that faded momentum by >= sigma (the new live rule blocks >=1.0 sigma fades).
"""
import glob
import json
import re
import sys
import time

LOGDIR = "/home/ubuntu/polybtc/logs"
NOW = time.time()
WINDOW_S = float(sys.argv[1]) if len(sys.argv) > 1 else 86400.0

# ---- outcomes: window -> "UP"/"DOWN" (scan all session logs) ----
out_re = re.compile(r"Bitcoin Up or Down - (.+?) -> (UP|DOWN) \| payout")
outcome = {}
for fp in glob.glob(f"{LOGDIR}/session_*.log"):
    try:
        with open(fp, errors="ignore") as f:
            for ln in f:
                m = out_re.search(ln)
                if m:
                    outcome[m.group(1).strip()] = m.group(2)
    except OSError:
        pass

# ---- filled snipe attempts from shadow_taker.jsonl ----
fills = []
with open(f"{LOGDIR}/shadow_taker.jsonl", errors="ignore") as f:
    for ln in f:
        try:
            r = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if r.get("type") != "attempt" or not r.get("filled"):
            continue
        if r.get("leg") != "snipe":
            continue
        if NOW - r.get("ts", 0) > WINDOW_S:
            continue
        title = r.get("title", "")
        key = title.split("Bitcoin Up or Down - ")[-1].strip()
        oc = outcome.get(key)
        if oc is None:
            continue
        side = r["side"]                       # "up"/"dn"
        sh = float(r.get("filled_shares") or 0)
        px = float(r.get("avg_fill_px") or r.get("limit_px") or 0)
        if sh <= 0 or px <= 0:
            continue
        won = (oc == "UP" and side == "up") or (oc == "DOWN" and side == "dn")
        fee = 0.07 * px * (1 - px) * sh
        pnl = (sh * (1 - px) - fee) if won else (-sh * px - fee)
        fills.append(dict(key=key, side=side, sh=sh, px=px, won=won, pnl=pnl,
                          tz=float(r.get("trend_z") or 0.0),
                          dist=float(r.get("dist_sigma") or 0.0),
                          trem=float(r.get("t_remaining_s") or 0.0),
                          ts=r["ts"]))

fills.sort(key=lambda x: x["ts"])


def book(fs, label):
    if not fs:
        print(f"{label:38} 0 fills")
        return
    n = len(fs)
    w = sum(f["won"] for f in fs)
    pnl = sum(f["pnl"] for f in fs)
    dep = sum(f["sh"] * f["px"] for f in fs)
    print(f"{label:38} {n:>3} fills  {w}/{n} won ({w/n:>4.0%})  "
          f"pnl ${pnl:>+7.2f}  dep ${dep:>6.0f}  ROI/$ {pnl/dep:>+5.1%}")


def fades(f, sig):
    """True if this fill bets AGAINST momentum by >= sig (blocked at this sigma)."""
    return (f["side"] == "up" and f["tz"] <= -sig) or \
           (f["side"] == "dn" and f["tz"] >= sig)


print(f"\n=== SNIPE scorecard, last {WINDOW_S/3600:.0f}h "
      f"({len(fills)} settled snipe fills) ===\n")
book(fills, "REALIZED (as traded, trend 1.5)")

for sig in (1.5, 1.0, 0.5, 0.0):
    kept = [f for f in fills if not fades(f, sig)]
    dropped = [f for f in fills if fades(f, sig)]
    dp = sum(f["pnl"] for f in dropped)
    book(kept, f"  if trend_filter={sig}  (drop {len(dropped)}, ${dp:+.2f})")

# side split + per-fill detail
print("\n  by side:")
for sd in ("up", "dn"):
    book([f for f in fills if f["side"] == sd], f"    {sd.upper()}")

print("\n  per-fill (ts / side / px / trend_z / dist / t_rem / won / pnl):")
for f in fills:
    flag = "  <-FADE>=1.0" if fades(f, 1.0) else ""
    print(f"    {time.strftime('%m-%d %H:%M', time.gmtime(f['ts']))} "
          f"{f['side'].upper():3} @{f['px']:.3f}  tz {f['tz']:+.2f}  "
          f"dist {f['dist']:+.2f}  trem {f['trem']:>5.0f}s  "
          f"{'WON ' if f['won'] else 'LOST'} ${f['pnl']:+6.2f}{flag}")
