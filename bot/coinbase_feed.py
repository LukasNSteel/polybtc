"""Coinbase Exchange spot BBO feed.

Chainlink BTC/USD aggregates multiple venues including Coinbase. Blending
Coinbase's top-of-book mid with Binance spot gives a sharper Chainlink proxy
for 5m/15m/4h resolution markets.
"""

import asyncio
import json
import logging
import time

import websockets

log = logging.getLogger("coinbase")

# Only escalate to WARNING after this many consecutive reconnect failures; a
# single idle-close that recovers on the next attempt stays at DEBUG.
WS_WARN_AFTER = 3

WS_URL = "wss://ws-feed.exchange.coinbase.com"


class CoinbaseFeed:
    MID_FRESH_SEC = 2.0

    def __init__(self, symbol: str = "BTC-USD"):
        self.symbol = symbol
        self.mid_price: float | None = None
        self.mid_ts: float = 0.0
        self.last_local_ts: float = 0.0

    @property
    def feed_age(self) -> float:
        return time.time() - self.last_local_ts if self.last_local_ts else float("inf")

    async def run(self) -> None:
        sub = {"type": "subscribe", "product_ids": [self.symbol], "channels": ["ticker"]}
        backoff = 2.0
        connected_once = False
        fails = 0
        while True:
            try:
                async with websockets.connect(WS_URL, ping_interval=20) as ws:
                    await ws.send(json.dumps(sub))
                    if fails >= WS_WARN_AFTER:
                        log.info("coinbase %s reconnected", self.symbol)
                    elif not connected_once:
                        log.info("connected to coinbase %s ticker", self.symbol)
                    connected_once = True
                    fails = 0
                    backoff = 2.0
                    async for msg in ws:
                        d = json.loads(msg)
                        if d.get("type") != "ticker" or d.get("product_id") != self.symbol:
                            continue
                        bid, ask = float(d.get("best_bid", 0)), float(d.get("best_ask", 0))
                        if bid <= 0 or ask <= 0:
                            continue
                        now = time.time()
                        self.mid_price = (bid + ask) / 2
                        self.mid_ts = now
                        self.last_local_ts = now
            except Exception as e:
                fails += 1
                if fails >= WS_WARN_AFTER:
                    log.warning("coinbase ws error: %s; retrying in %.0fs "
                                "(%d consecutive; binance spot still drives trading)",
                                e, backoff, fails)
                else:
                    log.debug("coinbase ws error: %s; retrying in %.0fs", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
