"""Fetch Binance BTCUSDT 1m klines and resolve each 5/15-min window UP/DOWN.
Used to score snipe signals that did NOT fill (no SETTLE line) since the
2026-06-25 03:56 UTC redeploy. Window labels are ET (UTC-4, EDT)."""
import urllib.request
import json
import datetime

# 2026-06-25 04:00:00 UTC == 12:00AM ET. Fetch 03:55 -> 05:05 UTC.
url = ("https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1m"
       "&startTime=1782359700000&endTime=1782363900000")
d = json.load(urllib.request.urlopen(url, timeout=20))
mins = {k[0] // 1000: (float(k[1]), float(k[4])) for k in d}  # epoch(s) -> (open, close)
print(f"fetched {len(d)} klines")


def et_label(epoch):
    et = datetime.datetime.utcfromtimestamp(epoch) - datetime.timedelta(hours=4)
    h = et.hour % 12 or 12
    ap = "AM" if et.hour < 12 else "PM"
    return f"{h}:{et.minute:02d}{ap}"


# (UTC start epoch, length minutes)
wins = [
    (1782359700, 5), (1782360000, 5), (1782360300, 5), (1782360600, 5),
    (1782360900, 5), (1782361800, 5), (1782361800, 15), (1782363000, 5),
    (1782363300, 5),
]
print(f"{'ET window':18} {'open':>10} {'close':>10} {'result':>7}")
for s, ln in wins:
    o = mins.get(s)
    cend = mins.get(s + (ln - 1) * 60)
    if not o or not cend:
        print(f"{et_label(s)} +{ln}m: missing kline (o={bool(o)} c={bool(cend)})")
        continue
    op, cl = o[0], cend[1]
    res = "UP" if cl > op else "DOWN"
    label = f"{et_label(s)}-{et_label(s + ln * 60)}"
    print(f"{label:18} {op:>10.1f} {cl:>10.1f} {res:>7}")
