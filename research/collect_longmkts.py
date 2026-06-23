"""Download a large, clean sample of resolved BTC up/down 15m / 1h / 4h markets
(plus a few days of 5m as a control) so the late-window question can be tested
on real data — the existing parquet archive is 5m-only.

For each market we save: kind, slug, window start/end, up/down token ids, the
realised outcome (ground truth from Gamma), and the UP-token 1-minute price
history from the CLOB. We also pull any missing Binance 1s daily kline zips so
the model (spot / candle-open / vol) lines up second-for-second.

Outputs (research/data/longmkts/):
  markets.csv         one row per resolved market
  prices.csv.gz       slug, ts, p_up  (1-min CLOB price history, UP token)
and Binance daily zips into research/data/binance_1s/.

Run:  .venv/bin/python research/collect_longmkts.py
"""
import asyncio
import csv
import datetime as dt
import gzip
import json
import os
import ssl
import urllib.request

import aiohttp
import truststore

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
BINVISION = "https://data.binance.vision/data/spot/daily/klines/BTCUSDT/1s"

OUT = "research/data/longmkts"
KDIR = "research/data/binance_1s"

# Window to collect. Keep END before "today" so every market is fully closed and
# every Binance daily zip exists. Aligned with the local 1s coverage that ends
# 2026-05-18, so the combined series stays contiguous.
START_DATE = dt.date(2026, 5, 19)
END_DATE = dt.date(2026, 6, 21)        # inclusive last full day
FIVE_M_CONTROL_DAYS = 4                 # only a few days of 5m (sanity check)

sem = asyncio.Semaphore(10)


def epoch(d: dt.date) -> int:
    return int(dt.datetime(d.year, d.month, d.day, tzinfo=dt.timezone.utc).timestamp())


async def get_json(s, url, params=None, retries=4):
    for i in range(retries):
        try:
            async with sem:
                async with s.get(url, params=params,
                                 timeout=aiohttp.ClientTimeout(total=30)) as r:
                    if r.status == 429:
                        await asyncio.sleep(2 * (i + 1))
                        continue
                    r.raise_for_status()
                    return await r.json()
        except Exception as e:
            if i == retries - 1:
                print(f"FAIL {url} {params}: {e}", flush=True)
                return None
            await asyncio.sleep(1.5 * (i + 1))
    return None


def parse_market(ev, kind, ws, we):
    try:
        m = ev["markets"][0]
        if not m.get("closed"):
            return None
        prices = json.loads(m.get("outcomePrices") or "[]")
        outcomes = json.loads(m.get("outcomes") or "[]")
        toks = json.loads(m.get("clobTokenIds") or "[]")
        if len(prices) != 2 or len(toks) != 2:
            return None
        up_i = outcomes.index("Up") if "Up" in outcomes else 0
        return {
            "kind": kind, "slug": m["slug"], "window_start": ws, "window_end": we,
            "condition_id": m["conditionId"],
            "up_token": toks[up_i], "down_token": toks[1 - up_i],
            "outcome_up": 1 if float(prices[up_i]) > 0.5 else 0,
            "volume": m.get("volumeNum") or 0.0,
        }
    except Exception as e:
        print(f"parse fail {kind} {ws}: {e}", flush=True)
        return None


async def fetch_epoch_markets(s, kind, window_sec, start_ep, end_ep):
    epochs = list(range(start_ep // window_sec * window_sec, end_ep, window_sec))
    out, B = [], 20
    for i in range(0, len(epochs), B):
        batch = epochs[i:i + B]
        qs = "&".join(f"slug=btc-updown-{kind}-{e}" for e in batch)
        evs = await get_json(s, f"{GAMMA}/events?{qs}")
        if evs:
            by_slug = {e["slug"]: e for e in evs}
            for e in batch:
                ev = by_slug.get(f"btc-updown-{kind}-{e}")
                if ev:
                    m = parse_market(ev, kind, e, e + window_sec)
                    if m:
                        out.append(m)
        if i % 400 == 0:
            print(f"{kind}: {i}/{len(epochs)} epochs, {len(out)} mkts", flush=True)
    print(f"{kind}: {len(out)} markets", flush=True)
    return out


async def fetch_hourly_markets(s, start_ep, end_ep):
    out, offset = [], 0
    while True:
        evs = await get_json(s, f"{GAMMA}/events",
                             {"series_id": "10114", "closed": "true", "limit": "100",
                              "offset": str(offset), "order": "endDate",
                              "ascending": "false"})
        if not evs:
            break
        done = False
        for ev in evs:
            ed = ev.get("endDate")
            if not ed:
                continue
            end_ts = int(dt.datetime.fromisoformat(ed.replace("Z", "+00:00")).timestamp())
            if end_ts < start_ep:
                done = True
                break
            if end_ts > end_ep:
                continue
            m = parse_market(ev, "1h", end_ts - 3600, end_ts)
            if m:
                out.append(m)
        offset += 100
        if done or len(evs) < 100:
            break
    print(f"1h: {len(out)} markets", flush=True)
    return out


async def fetch_prices(s, m):
    h = await get_json(s, f"{CLOB}/prices-history",
                       {"market": m["up_token"],
                        "startTs": str(m["window_start"] - 120),
                        "endTs": str(m["window_end"] + 60), "fidelity": "1"})
    if not h:
        return []
    return [(m["slug"], p["t"], p["p"]) for p in h.get("history", [])]


def dl_binance_zips():
    os.makedirs(KDIR, exist_ok=True)
    d = START_DATE
    while d <= END_DATE:
        fn = f"{KDIR}/BTCUSDT-1s-{d.isoformat()}.zip"
        if not os.path.exists(fn):
            url = f"{BINVISION}/BTCUSDT-1s-{d.isoformat()}.zip"
            try:
                urllib.request.urlretrieve(url, fn)
                print(f"binance {d}", flush=True)
            except Exception as e:
                print(f"binance MISS {d}: {e}", flush=True)
        d += dt.timedelta(days=1)


async def main():
    os.makedirs(OUT, exist_ok=True)
    print("downloading missing binance 1s zips...", flush=True)
    dl_binance_zips()

    ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    async with aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(ssl=ctx, limit=20)) as s:
        s_ep, e_ep = epoch(START_DATE), epoch(END_DATE + dt.timedelta(days=1))
        five_start = epoch(END_DATE + dt.timedelta(days=1) - dt.timedelta(days=FIVE_M_CONTROL_DAYS))
        m15, m4h, m1h, m5 = await asyncio.gather(
            fetch_epoch_markets(s, "15m", 900, s_ep, e_ep),
            fetch_epoch_markets(s, "4h", 14400, s_ep, e_ep),
            fetch_hourly_markets(s, s_ep, e_ep),
            fetch_epoch_markets(s, "5m", 300, five_start, e_ep),
        )
        markets = m15 + m4h + m1h + m5
        with open(f"{OUT}/markets.csv", "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(markets[0].keys()))
            w.writeheader()
            w.writerows(markets)
        print(f"MARKETS: 15m={len(m15)} 4h={len(m4h)} 1h={len(m1h)} 5m={len(m5)} "
              f"total={len(markets)}", flush=True)

        print("fetching prices-history...", flush=True)
        rows = []
        tasks = [fetch_prices(s, m) for m in markets]
        for i, fut in enumerate(asyncio.as_completed(tasks)):
            rows.extend(await fut)
            if i % 500 == 0:
                print(f"prices: {i}/{len(markets)} mkts, {len(rows)} pts", flush=True)
        with gzip.open(f"{OUT}/prices.csv.gz", "wt", newline="") as f:
            w = csv.writer(f)
            w.writerow(["slug", "ts", "p_up"])
            w.writerows(rows)
        print(f"PRICES: {len(rows)} points -> {OUT}/prices.csv.gz", flush=True)
    print("DONE", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
