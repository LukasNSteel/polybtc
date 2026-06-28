"""Re-score the live fills since the 5m-only deploy under a higher dist_sigma_min:
which fills had dist_sigma < new_floor (would be GATED), and the PnL of the
survivors vs the actual. dist_sigma was logged at fire on every attempt.
"""
import calendar, glob, json, re, sys, time

LOGDIR = "/home/ubuntu/polybtc/logs"
DEPLOY = calendar.timegm(time.strptime("2026-06-27 08:09:31", "%Y-%m-%d %H:%M:%S"))
FLOOR = float(sys.argv[1]) if len(sys.argv) > 1 else 0.7

out_re = re.compile(r"Bitcoin Up or Down - (.+?) -> (UP|DOWN) \| payout")
outcome = {}
for fp in glob.glob(f"{LOGDIR}/session_*.log"):
    try:
        for ln in open(fp, errors="ignore"):
            m = out_re.search(ln)
            if m:
                outcome[m.group(1).strip()] = m.group(2)
    except OSError:
        pass

att = []
for ln in open(f"{LOGDIR}/shadow_taker.jsonl", errors="ignore"):
    try:
        r = json.loads(ln)
    except json.JSONDecodeError:
        continue
    if r.get("type") == "attempt" and r.get("leg") == "snipe" and r.get("filled") and r.get("ts", 0) >= DEPLOY:
        att.append(r)


def wstart(slug):
    try:
        return int(slug.rsplit("-", 1)[1])
    except (ValueError, AttributeError, IndexError):
        return None

import urllib.request
starts = [wstart(a.get("slug")) for a in att if wstart(a.get("slug"))]
kl = {}
if starts:
    url = ("https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=5m"
           f"&startTime={min(starts)*1000}&endTime={(max(starts)+300)*1000}&limit=1000")
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            for k in json.load(resp):
                kl[k[0] // 1000] = "UP" if float(k[4]) >= float(k[1]) else "DOWN"
    except Exception as e:  # noqa: BLE001
        print("(kline fail", e, ")")


def res(a):
    key = a.get("title", "").split("Bitcoin Up or Down - ")[-1].strip()
    return outcome.get(key) or kl.get(wstart(a.get("slug")))

print(f"=== re-score {len(att)} fills under dist_sigma_min={FLOOR} ===")
print(f"{'time':12} {'dσ':>5} {'px':>6} {'out':>5} {'pnl':>7}  {'gated?':>7}")
act = keep = gated_w = gated_l = kept_w = kept_l = 0
act_pnl = keep_pnl = 0.0
for a in sorted(att, key=lambda x: x["ts"]):
    oc = res(a)
    if oc is None:
        continue
    ds = float(a.get("dist_sigma") or 0); px = float(a.get("avg_fill_px") or 0)
    sh = float(a.get("filled_shares") or 0)
    won = (oc == "UP") == (a["side"] == "up")
    fee = 0.07 * px * (1 - px) * sh
    pnl = (sh * (1 - px) - fee) if won else (-sh * px - fee)
    act += 1; act_pnl += pnl
    gated = ds < FLOOR
    if gated:
        gated_w += won; gated_l += (not won)
    else:
        keep += 1; keep_pnl += pnl; kept_w += won; kept_l += (not won)
    print(f"{time.strftime('%m-%d %H:%M', time.gmtime(a['ts'])):12} {ds:>5.2f} {px:>6.3f} "
          f"{('WON' if won else 'LOST'):>5} {pnl:>+7.2f}  {'GATED' if gated else '   -':>7}")

print(f"\nACTUAL (floor 0.5):  {act} fills  ->  PnL ${act_pnl:+.2f}")
ng = act - keep
print(f"GATED OUT (<{FLOOR}): {ng} fills  (of which {gated_w} won, {gated_l} lost)")
print(f"SURVIVING (>={FLOOR}): {keep} fills  ({kept_w}W/{kept_l}L, "
      f"{kept_w/keep:.0%} win)  ->  PnL ${keep_pnl:+.2f}" if keep else "none survive")
