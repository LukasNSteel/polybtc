"""Build the per-trade feature table for the filter model.

One row per settled snipe FILL with: direction, edge, distance, time-to-close,
trend (recorded live trend_z where available + a Binance-kline momentum proxy so
every row has a homogeneous trend feature), and the binary win + realized pnl.

Writes logs/filter_features.csv (copy it off-box and fit the model locally).
Run on the box:  .venv/bin/python -m research.build_filter_features
"""
import json
import re
import csv
import math
import bisect
import datetime
import urllib.request
import statistics as st
from glob import glob

SETTLE = re.compile(r"SETTLE (.+?) -> (UP|DOWN) ")
SNIPE = re.compile(
    r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+ .*SNIPE (.+?) (UP|DN): ask "
    r"[\d.]+ \(limit [\d.]+\) \+ fee [\d.]+ vs robust [\d.]+ "
    r"\(blend [\d.]+, edge ([\d.]+),")
LOOKBACK = 120  # seconds, momentum proxy window


def parse_ts(s):
    return datetime.datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(
        tzinfo=datetime.timezone.utc).timestamp()


def load_logs():
    settles, snipes = {}, []
    for path in glob("logs/session_*.log"):
        for line in open(path, errors="ignore"):
            ms = SETTLE.search(line)
            if ms:
                settles[ms.group(1).strip()] = "up" if ms.group(2) == "UP" else "dn"
            mf = SNIPE.search(line)
            if mf:
                snipes.append({"ts": parse_ts(mf.group(1)),
                               "title": mf.group(2).strip(),
                               "side": mf.group(3).lower(),
                               "edge": float(mf.group(4))})
    return settles, snipes


def match_edge(fill, snipes):
    best, bestdt = None, 15.0
    for s in snipes:
        if s["title"] == fill["title"] and s["side"] == fill["side"]:
            dt = abs(s["ts"] - fill["ts"])
            if dt < bestdt:
                best, bestdt = s["edge"], dt
    return best


def fee_per_share(px):
    return 0.07 * (px * (1 - px))


def fetch_klines(lo, hi):
    kl = {}
    t = lo
    while t < hi:
        url = (f"https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1m"
               f"&startTime={t * 1000}&endTime={min(hi, t + 1000 * 60) * 1000}"
               f"&limit=1000")
        data = json.load(urllib.request.urlopen(url, timeout=30))
        if not data:
            break
        for k in data:
            kl[k[0] // 1000] = float(k[4])
        t = data[-1][0] // 1000 + 60
    return kl


def main():
    settles, snipes = load_logs()
    raw = []
    for line in open("logs/shadow_taker.jsonl", errors="ignore"):
        try:
            r = json.loads(line)
        except Exception:
            continue
        if (r.get("type") != "attempt" or not r.get("filled")
                or r.get("leg") != "snipe"):
            continue
        outc = settles.get((r.get("title") or "").strip())
        dist, sh, px = r.get("dist_sigma"), r.get("filled_shares"), r.get("avg_fill_px")
        if outc is None or dist is None or not sh or not px:
            continue
        edge = match_edge(r, snipes)
        if edge is None:
            continue
        raw.append((r, outc, dist, sh, px, edge))

    if not raw:
        print("no trades")
        return
    lo = int(min(r[0]["ts"] for r in raw)) - LOOKBACK - 120
    hi = int(max(r[0]["ts"] for r in raw)) + 120
    kl = fetch_klines(lo, hi)
    ks = sorted(kl)
    rets = [math.log(kl[ks[i]] / kl[ks[i - 1]]) for i in range(1, len(ks))
            if kl[ks[i - 1]] > 0]
    vol1m = st.pstdev(rets) or 1e-9

    def price_at(ts):
        i = bisect.bisect_right(ks, ts) - 1
        return kl[ks[i]] if i >= 0 else None

    def momentum_z(ts):
        a, b = price_at(ts), price_at(ts - LOOKBACK)
        if not a or not b:
            return 0.0
        return math.log(a / b) / (vol1m * math.sqrt(LOOKBACK / 60))

    rows = []
    for r, outc, dist, sh, px, edge in raw:
        side = r["side"]
        won = int(side == outc)
        fee = sh * fee_per_share(px)
        pnl = (sh * (1 - px) - fee) if won else -(sh * px + fee)
        z = momentum_z(r["ts"])
        # trend toward the bet: + = momentum WITH the bet, - = fading it
        aligned = z if side == "up" else -z
        rows.append({
            "ts": round(r["ts"], 1), "dir": side, "is_up": int(side == "up"),
            "edge": round(edge, 4), "dist": round(dist, 3),
            "t_rem": round(r.get("t_remaining_s") or 0, 1),
            "trend_proxy": round(z, 3), "trend_aligned": round(aligned, 3),
            "trend_rec": (round(r["trend_z"], 3) if r.get("trend_z") is not None
                          else ""),
            "ask": round(px, 4), "won": won, "pnl": round(pnl, 3),
            "risk": round(sh * px + fee, 3), "kind": r.get("kind"),
        })

    out = "logs/filter_features.csv"
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    nwin = sum(r["won"] for r in rows)
    print(f"wrote {out}: {len(rows)} trades, {nwin} wins "
          f"({nwin / len(rows) * 100:.0f}%), klines {len(ks)}, "
          f"recorded trend_z on {sum(1 for r in rows if r['trend_rec'] != '')}")


if __name__ == "__main__":
    main()
