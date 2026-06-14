import json, glob, collections, datetime, statistics

rows = []
for f in glob.glob("research/activity_*.json"):
    d = json.load(open(f))
    if isinstance(d, list):
        rows += d
# dedupe by tx hash + asset + type + timestamp
seen = set()
uniq = []
for r in rows:
    k = (r["transactionHash"], r.get("asset"), r["type"], r["timestamp"], r.get("size"))
    if k in seen: continue
    seen.add(k)
    uniq.append(r)
rows = sorted(uniq, key=lambda r: r["timestamp"])
print(f"total events: {len(rows)}")
print("date range:", datetime.datetime.utcfromtimestamp(rows[0]["timestamp"]), "->",
      datetime.datetime.utcfromtimestamp(rows[-1]["timestamp"]))

by_type = collections.Counter(r["type"] for r in rows)
print("\nevent types:", dict(by_type))

rebates = [r for r in rows if r["type"] == "MAKER_REBATE"]
print(f"\nMAKER_REBATE: n={len(rebates)} total=${sum(r['usdcSize'] for r in rebates):,.2f}")

trades = [r for r in rows if r["type"] == "TRADE"]
print(f"\nTRADES: n={len(trades)}")
buys = [t for t in trades if t["side"] == "BUY"]
sells = [t for t in trades if t["side"] == "SELL"]
print(f"buys: {len(buys)} (${sum(t['usdcSize'] for t in buys):,.2f}), sells: {len(sells)} (${sum(t['usdcSize'] for t in sells):,.2f})")

# market categories
def cat(title):
    t = title.lower()
    if "up or down" in t:
        # distinguish recurrence
        return "up-or-down"
    if "above" in t:
        return "above-threshold"
    return "other"

cats = collections.Counter(cat(t["title"]) for t in trades)
print("\ntrade market categories:", dict(cats))

# asset mentioned
def asset_of(title):
    t = title.lower()
    for a in ["bitcoin", "ethereum", "solana", "xrp"]:
        if a in t: return a
    return "other"
print("assets:", dict(collections.Counter(asset_of(t["title"]) for t in trades)))

# price distribution of buys
print("\nBUY price buckets (count, $volume):")
buckets = collections.defaultdict(lambda: [0, 0.0])
for t in buys:
    p = t["price"]
    if p >= 0.99: b = "0.99+"
    elif p >= 0.95: b = "0.95-0.99"
    elif p >= 0.80: b = "0.80-0.95"
    elif p >= 0.60: b = "0.60-0.80"
    elif p >= 0.40: b = "0.40-0.60"
    elif p >= 0.20: b = "0.20-0.40"
    elif p >= 0.05: b = "0.05-0.20"
    else: b = "<0.05"
    buckets[b][0] += 1
    buckets[b][1] += t["usdcSize"]
for b in ["<0.05","0.05-0.20","0.20-0.40","0.40-0.60","0.60-0.80","0.80-0.95","0.95-0.99","0.99+"]:
    c, v = buckets[b]
    print(f"  {b:>10}: {c:5d} trades  ${v:,.2f}")

print("\nSELL price buckets (count, $volume):")
buckets = collections.defaultdict(lambda: [0, 0.0])
for t in sells:
    p = t["price"]
    if p >= 0.99: b = "0.99+"
    elif p >= 0.95: b = "0.95-0.99"
    elif p >= 0.80: b = "0.80-0.95"
    elif p >= 0.60: b = "0.60-0.80"
    elif p >= 0.40: b = "0.40-0.60"
    elif p >= 0.20: b = "0.20-0.40"
    elif p >= 0.05: b = "0.05-0.20"
    else: b = "<0.05"
    buckets[b][0] += 1
    buckets[b][1] += t["usdcSize"]
for b in ["<0.05","0.05-0.20","0.20-0.40","0.40-0.60","0.60-0.80","0.80-0.95","0.95-0.99","0.99+"]:
    c, v = buckets[b]
    print(f"  {b:>10}: {c:5d} trades  ${v:,.2f}")

# trade size stats
sizes = [t["usdcSize"] for t in trades]
print(f"\ntrade usdc size: median=${statistics.median(sizes):.2f} mean=${statistics.mean(sizes):.2f} max=${max(sizes):,.2f}")

# time-of-day distribution (ET = UTC-4)
hours = collections.Counter((datetime.datetime.utcfromtimestamp(t["timestamp"]).hour - 4) % 24 for t in trades)
print("\ntrades by hour (ET):", [f"{h}:{hours.get(h,0)}" for h in range(24)])

# trades per market
per_mkt = collections.Counter(t["title"] for t in trades)
print("\ntop 15 markets by trade count:")
for m, c in per_mkt.most_common(15):
    print(f"  {c:4d}  {m}")

# how many distinct markets
print(f"\ndistinct markets traded: {len(per_mkt)}")

# inter-trade timing within same market: buy->sell roundtrips?
print("\nsample of TRADE rows in one heavy market:")
heavy = per_mkt.most_common(1)[0][0]
for t in [x for x in trades if x["title"] == heavy][:30]:
    dt = datetime.datetime.utcfromtimestamp(t["timestamp"])
    print(f"  {dt} {t['side']:4} {t['outcome']:>4} p={t['price']:.3f} sz={t['size']:.1f} ${t['usdcSize']:.2f}")
