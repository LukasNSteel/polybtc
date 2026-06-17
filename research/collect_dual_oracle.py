"""Capture synchronized BTC trade/BBO ticks from Binance and Kraken at the same
time, recording BOTH the exchange timestamp and the local receive timestamp.

Why both clocks:
  - exchange_ts lets us measure *price-discovery lead-lag* between the two venues
    in a location-independent way (does Binance's tape move before Kraken's?).
  - recv_ts (one local clock for both feeds) is a cross-check on relative arrival,
    but it reflects THIS machine's network paths, not a Dublin VPS's.

The Dublin go-live decision (Binance-from-Tokyo vs Kraken-from-London) is then
made by `analyze_dual_oracle.py`, which combines the measured information lead
with modeled Dublin delivery latencies.

Usage: python research/collect_dual_oracle.py [seconds] [out.csv]
       (defaults: 600s -> research/data/dual_oracle_<ts>.csv)
"""

import asyncio
import csv
import json
import logging
import ssl
import sys
import time
from datetime import datetime

import aiohttp
import truststore
import websockets

truststore.inject_into_ssl()  # use the OS trust store (matches curl/browser behavior)
SSL_CTX = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)  # explicit ctx for aiohttp

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
log = logging.getLogger("dual")

BINANCE_WS = "wss://data-stream.binance.vision/stream?streams=btcusdt@trade/btcusdt@bookTicker"
BINANCE_DEPTH_WS = "wss://data-stream.binance.vision/stream?streams=btcusdt@depth@100ms"
BINANCE_DEPTH_SNAP = "https://data-api.binance.vision/api/v3/depth?symbol=BTCUSDT&limit=100"
KRAKEN_WS = "wss://ws.kraken.com/v2"

DURATION = float(sys.argv[1]) if len(sys.argv) > 1 else 600.0
OUT = sys.argv[2] if len(sys.argv) > 2 else f"research/data/dual_oracle_{int(time.time())}.csv"


def parse_kraken_ts(s: str) -> float:
    # v2 timestamps look like "2026-06-17T03:25:01.123456Z"
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


class Recorder:
    def __init__(self, path: str):
        self.f = open(path, "w", newline="")
        self.w = csv.writer(self.f)
        self.w.writerow(["src", "kind", "exch_ts", "recv_ts", "price", "qty"])
        self.lock = asyncio.Lock()
        self.n = {"binance": 0, "kraken": 0}

    async def row(self, src, kind, exch_ts, recv_ts, price, qty):
        async with self.lock:
            self.w.writerow([src, kind, f"{exch_ts:.6f}", f"{recv_ts:.6f}",
                             f"{price:.2f}", f"{qty:.8f}"])
            self.n[src] += 1

    def close(self):
        self.f.close()


async def binance_task(rec: Recorder, stop: float):
    while time.time() < stop:
        try:
            async with websockets.connect(BINANCE_WS, ping_interval=20) as ws:
                log.info("binance connected")
                async for msg in ws:
                    recv = time.time()
                    if recv >= stop:
                        return
                    d = json.loads(msg)
                    stream, data = d.get("stream", ""), d.get("data", {})
                    if stream.endswith("@trade"):
                        await rec.row("binance", "trade", data["T"] / 1000.0,
                                      recv, float(data["p"]), float(data["q"]))
                    elif stream.endswith("@bookTicker"):
                        bid, ask = float(data["b"]), float(data["a"])
                        if bid > 0 and ask > 0:
                            # bookTicker carries no exchange ts; use recv for both
                            await rec.row("binance", "bbo", recv, recv,
                                          (bid + ask) / 2, 0.0)
        except Exception as e:
            log.warning("binance ws error: %s; reconnecting", e)
            await asyncio.sleep(1)


async def binance_depth_task(rec: Recorder, stop: float):
    """Maintain Binance top-of-book from the diff-depth stream so we get a mid
    stamped with the exchange event time E (matching Kraken's book channel).
    Uses the standard REST-snapshot + diff sync."""
    while time.time() < stop:
        try:
            async with websockets.connect(BINANCE_DEPTH_WS, ping_interval=20) as ws:
                async with aiohttp.ClientSession() as s:
                    async with s.get(BINANCE_DEPTH_SNAP, ssl=SSL_CTX,
                                     timeout=aiohttp.ClientTimeout(total=10)) as r:
                        snap = await r.json()
                last_id = snap["lastUpdateId"]
                bids = {float(p): float(q) for p, q in snap["bids"]}
                asks = {float(p): float(q) for p, q in snap["asks"]}
                synced = False
                log.info("binance depth synced @ %d", last_id)
                async for msg in ws:
                    recv = time.time()
                    if recv >= stop:
                        return
                    d = json.loads(msg)["data"]
                    U, u, E = d["U"], d["u"], d["E"]
                    if u <= last_id:
                        continue
                    if not synced:
                        if not (U <= last_id + 1 <= u):
                            continue
                        synced = True
                    for p, q in d["b"]:
                        p, q = float(p), float(q)
                        bids[p] = q
                        if q == 0:
                            bids.pop(p, None)
                    for p, q in d["a"]:
                        p, q = float(p), float(q)
                        asks[p] = q
                        if q == 0:
                            asks.pop(p, None)
                    last_id = u
                    if bids and asks:
                        await rec.row("binance", "bbo_exch", E / 1000.0, recv,
                                      (max(bids) + min(asks)) / 2, 0.0)
        except Exception as e:
            log.warning("binance depth error: %r; reconnecting", e)
            await asyncio.sleep(1)


async def kraken_task(rec: Recorder, stop: float):
    sub_trade = {"method": "subscribe", "params": {"channel": "trade", "symbol": ["BTC/USD"]}}
    sub_book = {"method": "subscribe", "params": {"channel": "book", "depth": 10, "symbol": ["BTC/USD"]}}
    while time.time() < stop:
        # local top-of-book state, rebuilt on each (re)connect from the snapshot
        bids: dict[float, float] = {}
        asks: dict[float, float] = {}
        try:
            async with websockets.connect(KRAKEN_WS, ping_interval=20) as ws:
                await ws.send(json.dumps(sub_trade))
                await ws.send(json.dumps(sub_book))
                log.info("kraken connected")
                async for msg in ws:
                    recv = time.time()
                    if recv >= stop:
                        return
                    d = json.loads(msg)
                    ch, typ = d.get("channel"), d.get("type")
                    if ch == "trade" and typ in ("update", "snapshot"):
                        for t in d.get("data", []):
                            await rec.row("kraken", "trade", parse_kraken_ts(t["timestamp"]),
                                          recv, float(t["price"]), float(t["qty"]))
                    elif ch == "book" and typ in ("update", "snapshot"):
                        for b in d.get("data", []):
                            if typ == "snapshot":
                                bids = {float(x["price"]): float(x["qty"]) for x in b.get("bids", [])}
                                asks = {float(x["price"]): float(x["qty"]) for x in b.get("asks", [])}
                            else:
                                for x in b.get("bids", []):
                                    p, q = float(x["price"]), float(x["qty"])
                                    bids[p] = q
                                    if q == 0:
                                        bids.pop(p, None)
                                for x in b.get("asks", []):
                                    p, q = float(x["price"]), float(x["qty"])
                                    asks[p] = q
                                    if q == 0:
                                        asks.pop(p, None)
                            if not bids or not asks:
                                continue
                            mid = (max(bids) + min(asks)) / 2
                            ets = parse_kraken_ts(b["timestamp"]) if b.get("timestamp") else recv
                            await rec.row("kraken", "bbo", ets, recv, mid, 0.0)
        except Exception as e:
            log.warning("kraken ws error: %s; reconnecting", e)
            await asyncio.sleep(1)


async def main():
    rec = Recorder(OUT)
    stop = time.time() + DURATION
    log.info("capturing %.0fs -> %s", DURATION, OUT)
    try:
        await asyncio.gather(binance_task(rec, stop),
                             binance_depth_task(rec, stop),
                             kraken_task(rec, stop))
    finally:
        rec.close()
        log.info("done: binance=%d kraken=%d rows -> %s",
                 rec.n["binance"], rec.n["kraken"], OUT)


if __name__ == "__main__":
    asyncio.run(main())
