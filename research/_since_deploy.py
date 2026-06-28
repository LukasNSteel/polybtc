"""Fills strictly since the floor-size/UP-only deploy (2026-06-27 01:57:56 UTC),
with size in $ and shares so we can confirm floor sizing + read the recovery.
"""
import calendar, glob, json, re, time

LOGDIR = "/home/ubuntu/polybtc/logs"
DEPLOY = calendar.timegm(time.strptime("2026-06-27 01:57:56", "%Y-%m-%d %H:%M:%S"))

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

att, mk = {}, {}
for ln in open(f"{LOGDIR}/shadow_taker.jsonl", errors="ignore"):
    try:
        r = json.loads(ln)
    except json.JSONDecodeError:
        continue
    if r.get("type") == "attempt" and r.get("leg") == "snipe" and r.get("filled") and r.get("ts", 0) >= DEPLOY:
        att[r["id"]] = r
    elif r.get("type") == "markout" and r.get("drift_vs_fill") is not None:
        mk.setdefault(r["id"], {})[r["horizon_s"]] = r["drift_vs_fill"]

print(f"=== filled snipes since deploy ({time.strftime('%m-%d %H:%MZ', time.gmtime(DEPLOY))}) ===")
print(f"{'time':12} {'side':4} {'sh':>5} {'px':>6} {'$size':>6} {'mk10':>6} {'mk30':>6} {'mk60':>6} {'out':>5} {'pnl':>7}")
tot_pnl = tot_size = w = n = 0
for i in sorted(att, key=lambda i: att[i]["ts"]):
    a = att[i]
    sh = float(a.get("filled_shares") or 0); px = float(a.get("avg_fill_px") or 0)
    size = sh * px
    key = a.get("title", "").split("Bitcoin Up or Down - ")[-1].strip()
    oc = outcome.get(key)
    won = None if oc is None else ((oc == "UP") == (a["side"] == "up"))
    fee = 0.07 * px * (1 - px) * sh
    pnl = None if won is None else ((sh * (1 - px) - fee) if won else (-sh * px - fee))
    mm = mk.get(i, {})
    def c(h): 
        v = mm.get(h); return f"{v*100:+.1f}" if v is not None else "  -"
    o = "open" if won is None else ("WON" if won else "LOST")
    ps = f"{pnl:+.2f}" if pnl is not None else "  -"
    print(f"{time.strftime('%m-%d %H:%M', time.gmtime(a['ts'])):12} {a['side'].upper():4} "
          f"{sh:>5.0f} {px:>6.3f} {size:>6.2f} {c(10.0):>6} {c(30.0):>6} {c(60.0):>6} {o:>5} {ps:>7}")
    n += 1; tot_size += size
    if pnl is not None:
        tot_pnl += pnl; w += 1 if won else 0

settled = sum(1 for i in att if outcome.get(att[i].get("title","").split("Bitcoin Up or Down - ")[-1].strip()))
print(f"\n{n} fills  | avg size ${tot_size/n:.2f}" if n else "no fills")
if settled:
    print(f"settled {settled}: {w} won ({w/settled:.0%})  realized PnL ${tot_pnl:+.2f}")
