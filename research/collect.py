"""Collect resolved Polymarket BTC up/down markets + trades + price history + Binance klines.

Outputs (research/data/):
  markets.csv          one row per resolved market
  trades.csv.gz        all trades, tagged with slug/kind
  prices_1m.csv.gz     1-min price history for hourly/4h markets (UP token)
  binance_1s.csv.gz    1s klines covering the 5m/15m sample window
  binance_1m.csv.gz    1m klines covering the full 14d window
"""

import asyncio
import csv
import gzip
import io
import json
import ssl
import sys
import time

import aiohttp
import truststore

GAMMA = "https://gamma-api.polymarket.com"
DATA = "https://data-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
BINANCE = "https://data-api.binance.vision"

NOW = int(time.time())
DAY = 86400

# sample windows per market kind
FIVE_D = 3
FIFTEEN_D = 6
FOURH_D = 14
HOURLY_D = 14

OUT = "research/data"

sem = asyncio.Semaphore(10)


async def get_json(s, url, params=None, retries=4):
    for i in range(retries):
        try:
            async with sem:
                async with s.get(url, params=params, timeout=aiohttp.ClientTimeout(total=30)) as r:
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


def parse_market(ev, kind, window_start, window_end):
    try:
        m = ev["markets"][0]
        if not m.get("closed"):
            return None
        prices = json.loads(m.get("outcomePrices") or "[]")
        outcomes = json.loads(m.get("outcomes") or "[]")
        toks = json.loads(m.get("clobTokenIds") or "[]")
        if len(prices) != 2 or len(toks) != 2:
            return None
        # map to Up/Down explicitly
        up_i = outcomes.index("Up") if "Up" in outcomes else 0
        dn_i = 1 - up_i
        outcome_up = 1 if float(prices[up_i]) > 0.5 else 0
        return {
            "kind": kind,
            "slug": m["slug"],
            "window_start": window_start,
            "window_end": window_end,
            "condition_id": m["conditionId"],
            "up_token": toks[up_i],
            "down_token": toks[dn_i],
            "outcome_up": outcome_up,
            "volume": m.get("volumeNum") or 0.0,
            "fee_bps": m.get("takerBaseFee") or 0,
        }
    except Exception as e:
        print(f"parse fail {kind} {window_start}: {e}", flush=True)
        return None


async def fetch_epoch_markets(s, kind, window_sec, days):
    """Fixed-window markets discovered by slug epoch."""
    start = (NOW - days * DAY) // window_sec * window_sec
    end = (NOW - 2 * window_sec) // window_sec * window_sec  # only fully closed
    epochs = list(range(start, end, window_sec))
    out = []
    B = 20
    for i in range(0, len(epochs), B):
        batch = epochs[i : i + B]
        qs = "&".join(f"slug=btc-updown-{kind}-{e}" for e in batch)
        evs = await get_json(s, f"{GAMMA}/events?{qs}")
        if not evs:
            continue
        by_slug = {e["slug"]: e for e in evs}
        for e in batch:
            ev = by_slug.get(f"btc-updown-{kind}-{e}")
            if ev:
                m = parse_market(ev, kind, e, e + window_sec)
                if m:
                    out.append(m)
        if i % 200 == 0:
            print(f"{kind}: {i}/{len(epochs)} epochs, {len(out)} markets", flush=True)
    return out


async def fetch_hourly_markets(s, days):
    from datetime import datetime, timezone

    cutoff = NOW - days * DAY
    out = []
    offset = 0
    while True:
        evs = await get_json(
            s,
            f"{GAMMA}/events",
            {"series_id": "10114", "closed": "true", "limit": "100",
             "offset": str(offset), "order": "endDate", "ascending": "false"},
        )
        if not evs:
            break
        done = False
        for ev in evs:
            ed = ev.get("endDate")
            if not ed:
                continue
            end_ts = int(datetime.fromisoformat(ed.replace("Z", "+00:00")).timestamp())
            if end_ts < cutoff:
                done = True
                break
            if end_ts > NOW - 3600:
                continue
            m = parse_market(ev, "1h", end_ts - 3600, end_ts)
            if m:
                out.append(m)
        offset += 100
        print(f"1h: offset {offset}, {len(out)} markets", flush=True)
        if done or len(evs) < 100:
            break
    return out


async def fetch_trades(s, m):
    """All trades for a condition id, paginated."""
    rows = []
    offset = 0
    while offset <= 5000:
        t = await get_json(
            s, f"{DATA}/trades",
            {"market": m["condition_id"], "limit": "500", "offset": str(offset),
             "takerOnly": "true"},
        )
        if not t or not isinstance(t, list):
            break
        for tr in t:
            rows.append({
                "kind": m["kind"], "slug": m["slug"],
                "ts": tr.get("timestamp"), "side": tr.get("side"),
                "outcome": tr.get("outcome"), "price": tr.get("price"),
                "size": tr.get("size"),
            })
        if len(t) < 500:
            break
        offset += 500
    return rows


async def fetch_price_history(s, m):
    h = await get_json(
        s, f"{CLOB}/prices-history",
        {"market": m["up_token"], "startTs": str(m["window_start"] - 600),
         "endTs": str(m["window_end"] + 60), "fidelity": "1"},
    )
    if not h:
        return []
    return [{"kind": m["kind"], "slug": m["slug"], "ts": p["t"], "p_up": p["p"]}
            for p in h.get("history", [])]


async def fetch_binance(s, interval, start_ts, end_ts, fname):
    rows = []
    cur = start_ts * 1000
    end_ms = end_ts * 1000
    step = {"1s": 1000, "1m": 60000}[interval]
    while cur < end_ms:
        k = await get_json(
            s, f"{BINANCE}/api/v3/klines",
            {"symbol": "BTCUSDT", "interval": interval, "startTime": str(cur),
             "endTime": str(end_ms), "limit": "1000"},
        )
        if not k:
            break
        for c in k:
            rows.append({"ts": c[0] // 1000, "open": c[1], "high": c[2],
                         "low": c[3], "close": c[4], "volume": c[5]})
        if len(k) < 1000:
            break
        cur = k[-1][0] + step
        if len(rows) % 50000 == 0:
            print(f"binance {interval}: {len(rows)} rows", flush=True)
    write_csv(fname, rows)
    print(f"binance {interval}: {len(rows)} rows -> {fname}", flush=True)


def write_csv(fname, rows):
    if not rows:
        print(f"WARN no rows for {fname}", flush=True)
        return
    opener = gzip.open if fname.endswith(".gz") else open
    with opener(f"{OUT}/{fname}", "wt", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


async def main():
    ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ctx, limit=20)) as s:
        # 1. enumerate markets
        t0 = time.time()
        m5, m15, m4h, m1h = await asyncio.gather(
            fetch_epoch_markets(s, "5m", 300, FIVE_D),
            fetch_epoch_markets(s, "15m", 900, FIFTEEN_D),
            fetch_epoch_markets(s, "4h", 14400, FOURH_D),
            fetch_hourly_markets(s, HOURLY_D),
        )
        markets = m5 + m15 + m4h + m1h
        write_csv("markets.csv", markets)
        print(f"MARKETS: 5m={len(m5)} 15m={len(m15)} 4h={len(m4h)} 1h={len(m1h)} "
              f"total={len(markets)} in {time.time()-t0:.0f}s", flush=True)

        # 2. trades for all markets
        t0 = time.time()
        all_trades = []
        tasks = [fetch_trades(s, m) for m in markets]
        for i, fut in enumerate(asyncio.as_completed(tasks)):
            all_trades.extend(await fut)
            if i % 100 == 0:
                print(f"trades: {i}/{len(markets)} markets, {len(all_trades)} rows", flush=True)
        write_csv("trades.csv.gz", all_trades)
        print(f"TRADES: {len(all_trades)} rows in {time.time()-t0:.0f}s", flush=True)

        # 3. 1m price history for 1h and 4h markets
        t0 = time.time()
        ph = []
        long_mkts = [m for m in markets if m["kind"] in ("1h", "4h")]
        tasks = [fetch_price_history(s, m) for m in long_mkts]
        for i, fut in enumerate(asyncio.as_completed(tasks)):
            ph.extend(await fut)
            if i % 50 == 0:
                print(f"prices: {i}/{len(long_mkts)}", flush=True)
        write_csv("prices_1m.csv.gz", ph)
        print(f"PRICES: {len(ph)} rows in {time.time()-t0:.0f}s", flush=True)

        # 4. binance klines
        await fetch_binance(s, "1s", NOW - (FIVE_D + 1) * DAY, NOW, "binance_1s.csv.gz")
        await fetch_binance(s, "1m", NOW - (HOURLY_D + 1) * DAY, NOW, "binance_1m.csv.gz")

    print("DONE", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
