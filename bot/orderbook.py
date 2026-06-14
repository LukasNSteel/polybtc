"""Polymarket CLOB market-data websocket: live order books + last trades."""

import asyncio
import json
import logging
import time
from collections import defaultdict
from typing import Callable

import websockets

log = logging.getLogger("orderbook")

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


class Book:
    def __init__(self) -> None:
        self.bids: dict[float, float] = {}
        self.asks: dict[float, float] = {}
        self.ts: float = 0.0

    def best_bid(self) -> tuple[float, float] | None:
        if not self.bids:
            return None
        p = max(self.bids)
        return p, self.bids[p]

    def best_ask(self) -> tuple[float, float] | None:
        if not self.asks:
            return None
        p = min(self.asks)
        return p, self.asks[p]


class OrderBookFeed:
    """Maintains books for a dynamic set of asset ids.

    The CLOB ws takes the asset list at subscribe time, so when the set
    changes (new 5-minute market every 5 minutes) we reconnect.
    """

    def __init__(self) -> None:
        self.books: dict[str, Book] = defaultdict(Book)
        self._assets: set[str] = set()
        self._want_reconnect = asyncio.Event()
        # callbacks fired on each trade print: (asset_id, price, size)
        self.on_trade: list[Callable[[str, float, float], None]] = []

    def set_assets(self, assets: set[str]) -> None:
        if assets != self._assets:
            self._assets = set(assets)
            self._want_reconnect.set()

    def _handle(self, d: dict) -> None:
        et = d.get("event_type")
        asset = d.get("asset_id", "")
        if et == "book":
            book = self.books[asset]
            book.bids = {float(x["price"]): float(x["size"]) for x in d.get("bids", [])}
            book.asks = {float(x["price"]): float(x["size"]) for x in d.get("asks", [])}
            book.ts = time.time()
        elif et == "price_change":
            for ch in d.get("changes", []):
                asset_id = ch.get("asset_id") or asset
                book = self.books[asset_id]
                price, size = float(ch["price"]), float(ch["size"])
                side = ch.get("side", "")
                levels = book.bids if side == "BUY" else book.asks
                if size == 0:
                    levels.pop(price, None)
                else:
                    levels[price] = size
                book.ts = time.time()
        elif et == "last_trade_price":
            price, size = float(d.get("price", 0)), float(d.get("size", 0) or 0)
            for cb in self.on_trade:
                cb(asset, price, size)

    async def _pinger(self, ws) -> None:
        while True:
            await asyncio.sleep(10)
            await ws.send("PING")

    async def run(self) -> None:
        while True:
            if not self._assets:
                await asyncio.sleep(1)
                continue
            self._want_reconnect.clear()
            assets = sorted(self._assets)
            try:
                async with websockets.connect(WS_URL, ping_interval=None) as ws:
                    await ws.send(json.dumps({"type": "market", "assets_ids": assets}))
                    log.info("subscribed to %d assets", len(assets))
                    ping_task = asyncio.create_task(self._pinger(ws))
                    try:
                        while not self._want_reconnect.is_set():
                            try:
                                msg = await asyncio.wait_for(ws.recv(), timeout=1.0)
                            except asyncio.TimeoutError:
                                continue
                            if msg == "PONG":
                                continue
                            data = json.loads(msg)
                            for item in data if isinstance(data, list) else [data]:
                                self._handle(item)
                    finally:
                        ping_task.cancel()
            except Exception as e:
                log.warning("clob ws error: %s; reconnecting in 2s", e)
                await asyncio.sleep(2)
