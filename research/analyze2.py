import json, glob, collections, datetime

rows = []
for f in glob.glob("research/activity_*.json"):
    d = json.load(open(f))
    if isinstance(d, list):
        rows += d
seen = set(); uniq = []
for r in rows:
    k = (r["transactionHash"], r.get("asset"), r["type"], r["timestamp"], r.get("size"))
    if k in seen: continue
    seen.add(k); uniq.append(r)
rows = sorted(uniq, key=lambda r: r["timestamp"])
trades = [r for r in rows if r["type"] == "TRADE"]
redeems = [r for r in rows if r["type"] == "REDEEM"]
merges = [r for r in rows if r["type"] == "MERGE"]

# per market: cost of Up buys, cost of Down buys, shares each, redeem payout
mkt = collections.defaultdict(lambda: {"up_sh":0,"up_$":0,"dn_sh":0,"dn_$":0,"sell_$":0,"sell_sh":0,"redeem":0,"merge":0,"n":0,"t0":None,"t1":None})
for t in trades:
    m = mkt[t["title"]]
    m["n"] += 1
    ts = t["timestamp"]
    m["t0"] = ts if m["t0"] is None else min(m["t0"], ts)
    m["t1"] = ts if m["t1"] is None else max(m["t1"], ts)
    o = t["outcome"].lower()
    if t["side"] == "BUY":
        if o in ("up","yes"):
            m["up_sh"] += t["size"]; m["up_$"] += t["usdcSize"]
        else:
            m["dn_sh"] += t["size"]; m["dn_$"] += t["usdcSize"]
    else:
        m["sell_sh"] += t["size"]; m["sell_$"] += t["usdcSize"]
for r in redeems:
    mkt[r["title"]]["redeem"] += r["usdcSize"]
for r in merges:
    mkt[r["title"]]["merge"] += r["usdcSize"]

both = sum(1 for m in mkt.values() if m["up_sh"]>0 and m["dn_sh"]>0)
print(f"markets traded: {len(mkt)}, with BOTH sides bought: {both}")

tot_cost = tot_payout = 0
print(f"\n{'market':52s} {'n':>4} {'upSh':>8} {'up$':>8} {'dnSh':>8} {'dn$':>8} {'redeem':>9} {'pnl':>9}")
results = []
for name, m in sorted(mkt.items(), key=lambda kv: -(kv[1]['up_$']+kv[1]['dn_$'])):
    cost = m["up_$"] + m["dn_$"]
    payout = m["redeem"] + m["merge"] + m["sell_$"]
    pnl = payout - cost
    tot_cost += cost; tot_payout += payout
    results.append((name, m, pnl))
for name, m, pnl in results[:35]:
    cost = m["up_$"]+m["dn_$"]
    print(f"{name[:52]:52s} {m['n']:4d} {m['up_sh']:8.0f} {m['up_$']:8.0f} {m['dn_sh']:8.0f} {m['dn_$']:8.0f} {m['redeem']+m['merge']+m['sell_$']:9.0f} {pnl:9.1f}")

print(f"\nTOTAL: cost=${tot_cost:,.0f} payout=${tot_payout:,.0f} pnl=${tot_payout-tot_cost:,.1f}")
print("(plus $859.86 maker rebate)")

wins = sum(1 for _,_,p in results if p > 0)
print(f"profitable markets: {wins}/{len(results)}")

# average combined price paid when buying both sides:
# if you buy X up-shares at avg pu and Y down-shares at avg pd, hedged pairs = min(X,Y)
print("\nimplied avg prices per market (top 15 by volume):")
for name, m, pnl in results[:15]:
    pu = m["up_$"]/m["up_sh"] if m["up_sh"] else 0
    pd = m["dn_$"]/m["dn_sh"] if m["dn_sh"] else 0
    print(f"  {name[:50]:50s} avgUp={pu:.3f} avgDn={pd:.3f} sum={pu+pd:.3f}")

# trade timing vs market window end. parse window from title
import re
def window_minutes(title):
    # e.g. "May 4, 10:00AM-10:15AM ET" or "May 4, 9AM ET" (hourly)
    m = re.search(r'(\d{1,2}):(\d{2})(AM|PM)-(\d{1,2}):(\d{2})(AM|PM)', title)
    if m:
        h1,m1 = int(m.group(1))%12 + (12 if m.group(3)=='PM' else 0), int(m.group(2))
        h2,m2 = int(m.group(4))%12 + (12 if m.group(6)=='PM' else 0), int(m.group(5))
        return (h2*60+m2)-(h1*60+m1)
    return None
durs = collections.Counter(window_minutes(t["title"]) for t in trades)
print("\nmarket window durations (minutes -> trade count):", dict(durs))
