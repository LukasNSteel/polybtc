"""Full snipe funnel since the 5m-only deploy (2026-06-27 08:09:31 UTC):
attempts -> fills -> misses, and win/loss on settled fills. Outcomes for fills
come from the bot's own SETTLE lines (we held a position); missed-attempt and
DN-counterfactual outcomes are resolved from Binance 5m klines.
"""
import calendar, glob, json, re, time, urllib.request

LOGDIR = "/home/ubuntu/polybtc/logs"
DEPLOY = calendar.timegm(time.strptime("2026-06-27 08:09:31", "%Y-%m-%d %H:%M:%S"))
NOW = time.time()

# settlements the bot logged (markets we traded)
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

# all snipe attempts since deploy
att = []
for ln in open(f"{LOGDIR}/shadow_taker.jsonl", errors="ignore"):
    try:
        r = json.loads(ln)
    except json.JSONDecodeError:
        continue
    if r.get("type") == "attempt" and r.get("leg") == "snipe" and r.get("ts", 0) >= DEPLOY:
        att.append(r)

filled = [a for a in att if a.get("filled")]
missed = [a for a in att if not a.get("filled")]

# resolve fills via SETTLE lines; fall back to klines if missing
def kline_outcomes(starts):
    if not starts:
        return {}
    out = {}
    url = ("https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=5m"
           f"&startTime={min(starts)*1000}&endTime={(max(starts)+300)*1000}&limit=1000")
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            for k in json.load(resp):
                out[k[0] // 1000] = "UP" if float(k[4]) >= float(k[1]) else "DOWN"
    except Exception as e:  # noqa: BLE001
        print("  (kline fetch failed:", e, ")")
    return out


def wstart(slug):
    try:
        return int(slug.rsplit("-", 1)[1])
    except (ValueError, AttributeError, IndexError):
        return None

kl = kline_outcomes([wstart(a.get("slug")) for a in att if wstart(a.get("slug"))])


def settle(a):
    key = a.get("title", "").split("Bitcoin Up or Down - ")[-1].strip()
    oc = outcome.get(key)
    if oc is None:
        oc = kl.get(wstart(a.get("slug")))
    return oc

print(f"=== SNIPE FUNNEL since 5m-only deploy ({time.strftime('%m-%d %H:%MZ', time.gmtime(DEPLOY))}, "
      f"{(NOW-DEPLOY)/3600:.1f}h ago) ===\n")
print(f"  orders attempted : {len(att)}")
print(f"  filled           : {len(filled)}")
print(f"  missed (no fill) : {len(missed)}")
if att:
    print(f"  fill rate        : {len(filled)/len(att):.0%}")

# win/loss on fills
w = l = openn = pnl = size = 0
rows = []
for a in filled:
    oc = settle(a)
    sh = float(a.get("filled_shares") or 0); px = float(a.get("avg_fill_px") or 0)
    size += sh * px
    if oc is None:
        openn += 1; rows.append((a, "open", None)); continue
    won = (oc == "UP") == (a["side"] == "up")
    fee = 0.07 * px * (1 - px) * sh
    p = (sh * (1 - px) - fee) if won else (-sh * px - fee)
    pnl += p; w += 1 if won else 0; l += 0 if won else 1
    rows.append((a, "WON" if won else "LOST", p))

print(f"\n  FILLS settled    : {w+l}  ->  won {w}, lost {l}" + (f", open {openn}" if openn else ""))
if w + l:
    print(f"  win rate         : {w/(w+l):.0%}")
    print(f"  realized PnL     : ${pnl:+.2f}   (avg size ${size/len(filled):.2f})")

# what the misses would have done (recapture analysis, kline-resolved)
mw = ml = 0
for a in missed:
    oc = settle(a)
    if oc is None:
        continue
    if (oc == "UP") == (a["side"] == "up"):
        mw += 1
    else:
        ml += 1
if missed:
    print(f"\n  MISSES resolved  : {mw+ml}  ->  would-have-won {mw}, would-have-lost {ml}"
          + (f"  ({mw/(mw+ml):.0%} win)" if mw+ml else ""))

print(f"\n=== per-fill detail ===")
print(f"{'time':12} {'side':4} {'sh':>4} {'px':>6} {'$':>5} {'mk10':>6} {'mk60':>6} {'out':>5} {'pnl':>7}")
mk = {}
for ln in open(f"{LOGDIR}/shadow_taker.jsonl", errors="ignore"):
    try:
        r = json.loads(ln)
    except json.JSONDecodeError:
        continue
    if r.get("type") == "markout" and r.get("drift_vs_fill") is not None:
        mk.setdefault(r["id"], {})[r["horizon_s"]] = r["drift_vs_fill"]
for a, res, p in sorted(rows, key=lambda x: x[0]["ts"]):
    m = mk.get(a["id"], {})
    def c(h):
        v = m.get(h); return f"{v*100:+.1f}" if v is not None else "  -"
    sh = float(a.get("filled_shares") or 0); px = float(a.get("avg_fill_px") or 0)
    ps = f"{p:+.2f}" if p is not None else "  -"
    print(f"{time.strftime('%m-%d %H:%M', time.gmtime(a['ts'])):12} {a['side'].upper():4} "
          f"{sh:>4.0f} {px:>6.3f} {sh*px:>5.2f} {c(10.0):>6} {c(60.0):>6} {res:>5} {ps:>7}")
