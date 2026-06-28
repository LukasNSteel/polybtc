"""Counterfactual: what PnL would the PAUSED DN snipes have added since the DN
pause (snipe_sides=[up], deployed ~01:22 UTC 2026-06-27)? Reads
shadow_candidates.jsonl (reason='side' == DN fires we suppressed), dedupes to one
fire per market window (slug), resolves the TRUE outcome from Binance 5m klines
(close>=open -> UP wins; the bot logs no SETTLE line for markets it didn't
trade), and prices at the SAME floor size (5 shares) as the live UP fills.

IMPORTANT CAVEAT: this fills at the SEEN ASK — it does NOT model the DN execution
leak we measured (DN mid drops ~4.5c in 10s post-fill). So this is an OPTIMISTIC
upper bound for DN; the real DN PnL would very likely be WORSE.
"""
import json, time, urllib.request

LOGDIR = "/home/ubuntu/polybtc/logs"
SHARES = 5.0  # venue floor, same as live UP fills

# one DN suppressed fire per market window (first qualifying), since a real fire
# would not re-fire the same window every shadow-cooldown.
seen = {}
raw = 0
for ln in open(f"{LOGDIR}/shadow_candidates.jsonl", errors="ignore"):
    try:
        r = json.loads(ln)
    except json.JSONDecodeError:
        continue
    if r.get("reason") != "side" or r.get("side") != "dn":
        continue
    raw += 1
    seen.setdefault(r.get("slug"), r)

# window start epoch from slug btc-updown-5m-<startepoch>
def wstart(slug):
    try:
        return int(slug.rsplit("-", 1)[1])
    except (ValueError, AttributeError, IndexError):
        return None

starts = sorted(s for s in (wstart(sl) for sl in seen) if s)
# resolve outcomes from Binance 5m klines (one batched REST call over the range)
kl_open = {}
if starts:
    url = ("https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=5m"
           f"&startTime={starts[0]*1000}&endTime={(starts[-1]+300)*1000}&limit=1000")
    with urllib.request.urlopen(url, timeout=15) as resp:
        for k in json.load(resp):
            kl_open[k[0] // 1000] = (float(k[1]), float(k[4]))  # openTime->(open,close)

print(f"=== DN counterfactual since DN pause (~01:22Z 2026-06-27) ===")
print(f"raw DN 'side' candidate logs: {raw}  ->  {len(seen)} unique market windows")
print(f"(priced at {SHARES:.0f} shares = floor size; OPTIMISTIC: fills at seen ask, "
      f"ignores DN's -4.5c/10s post-fill markout)\n")
print(f"{'window(UTC)':16} {'px':>6} {'$size':>6} {'o->c':>10} {'out':>5} {'pnl':>7}")
tot = w = settled = 0
for slug, r in sorted(seen.items(), key=lambda kv: wstart(kv[0]) or 0):
    px = float(r.get("seen_ask_px") or 0)
    s = wstart(slug)
    oc = kl_open.get(s)
    wt = time.strftime("%m-%d %H:%M", time.gmtime(s)) if s else "?"
    if not px or oc is None:
        print(f"{wt:16} {px:>6.3f} {px*SHARES:>6.2f} {'(unsettled)':>10} {'open':>5} {'  -':>7}")
        continue
    op, cl = oc
    won = (cl < op)  # DOWN bet wins when close < open (ties -> Up)
    fee = 0.07 * px * (1 - px) * SHARES
    pnl = (SHARES * (1 - px) - fee) if won else (-SHARES * px - fee)
    settled += 1; w += 1 if won else 0; tot += pnl
    print(f"{wt:16} {px:>6.3f} {px*SHARES:>6.2f} {op:>5.0f}->{cl:<4.0f} "
          f"{('WON' if won else 'LOST'):>5} {pnl:>+7.2f}")

UP_ACTUAL = 4.75  # realized UP PnL since deploy (research/_since_deploy.py)
if settled:
    print(f"\nDN counterfactual: {settled} settled, {w} won ({w/settled:.0%})  PnL ${tot:+.2f}")
    print(f"UP actual (live):        +$4.75")
    print(f"BOTH-SIDES would-be PnL:  ${UP_ACTUAL + tot:+.2f}   (vs +$4.75 UP-only)")
    print(f"  -> DN {'ADDED' if tot>0 else 'COST'} ${abs(tot):.2f} even on the OPTIMISTIC "
          f"(no-execution-leak) basis")
else:
    print("\nno settled DN windows")
