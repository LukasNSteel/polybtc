"""Fetch the target account's full day-1 activity via start-cursor pagination."""

import datetime
import json
import time
import urllib.request

import truststore

truststore.inject_into_ssl()

WALLET = "0xe1d6b51521bd4365769199f392f9818661bd907c"
BASE = ("https://data-api.polymarket.com/activity?user={w}&limit=500"
        "&sortBy=TIMESTAMP&sortDirection=ASC&start={s}")
HDRS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Accept": "application/json"}

END_TS = 1774396800  # 2026-03-25 00:00 UTC — end of day 1

rows = json.load(open("research/activity_asc.json"))
start = rows[-1]["timestamp"]
for i in range(30):
    if start >= END_TS:
        break
    req = urllib.request.Request(BASE.format(w=WALLET, s=start), headers=HDRS)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            batch = json.load(r)
    except Exception as e:
        print("fail:", e, flush=True)
        time.sleep(3)
        continue
    if not batch:
        break
    rows += batch
    start = batch[-1]["timestamp"]
    print(f"{i}: +{len(batch)} -> {datetime.datetime.utcfromtimestamp(start)}", flush=True)
    time.sleep(1.0)

seen, uniq = set(), []
for r in rows:
    k = (r["transactionHash"], r.get("asset"), r["type"], r["timestamp"],
         r.get("size"), r.get("price"))
    if k in seen:
        continue
    seen.add(k)
    uniq.append(r)
uniq.sort(key=lambda r: r["timestamp"])
json.dump(uniq, open("research/activity_day1.json", "w"))
print("unique:", len(uniq), "| last:",
      datetime.datetime.utcfromtimestamp(uniq[-1]["timestamp"]), flush=True)
