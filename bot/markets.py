"""Discovery of active Polymarket BTC Up/Down markets via the Gamma API."""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field

import aiohttp

log = logging.getLogger("markets")

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
HOURLY_SERIES_ID = "10114"  # btc-up-or-down-hourly


@dataclass
class Market:
    slug: str
    title: str
    condition_id: str
    token_up: str
    token_down: str
    open_ts: int          # candle/window open (unix seconds)
    close_ts: int         # candle/window close (unix seconds)
    tick: float
    kind: str             # "5m" | "15m" | "1h" | "4h"
    interval: str         # binance kline interval for open-price lookup
    fee_rate: float = 0.07       # taker fee: rate * (p*(1-p))**exponent per share
    fee_exponent: float = 1.0
    accepting: bool = True
    open_price: float | None = None
    resolved: bool = field(default=False)

    @property
    def t_remaining(self) -> float:
        return self.close_ts - time.time()

    def taker_fee_per_share(self, price: float) -> float:
        return self.fee_rate * (price * (1 - price)) ** self.fee_exponent


def _parse_market(e: dict, kind: str, interval: str, open_ts: int, close_ts: int) -> Market | None:
    m = e["markets"][0]
    try:
        outcomes = json.loads(m["outcomes"])
        tokens = json.loads(m["clobTokenIds"])
    except (KeyError, json.JSONDecodeError):
        return None
    if not m.get("acceptingOrders", False):
        return None
    up_idx = outcomes.index("Up")
    dn_idx = 1 - up_idx
    fs = m.get("feeSchedule") or {}
    return Market(
        slug=e["slug"],
        title=e["title"],
        condition_id=m.get("conditionId", ""),
        token_up=tokens[up_idx],
        token_down=tokens[dn_idx],
        open_ts=open_ts,
        close_ts=close_ts,
        tick=float(m.get("orderPriceMinTickSize", 0.01)),
        kind=kind,
        interval=interval,
        fee_rate=float(fs.get("rate", 0.07)) if m.get("feesEnabled") else 0.0,
        fee_exponent=float(fs.get("exponent", 1.0)),
        accepting=True,
    )


class MarketManager:
    """Keeps `self.active` populated with currently tradable markets."""

    def __init__(self, enable_5m: bool, enable_hourly: bool, enable_15m: bool = False,
                 enable_4h: bool = False):
        self.enable_5m = enable_5m
        self.enable_15m = enable_15m
        self.enable_hourly = enable_hourly
        self.enable_4h = enable_4h
        self.active: dict[str, Market] = {}   # slug -> Market
        self.expired: list[Market] = []       # closed, awaiting settlement
        self._known_missing: set[str] = set()

    async def _fetch_event(self, session: aiohttp.ClientSession, **params) -> list[dict]:
        try:
            async with session.get(f"{GAMMA}/events", params=params,
                                   timeout=aiohttp.ClientTimeout(total=8)) as r:
                return await r.json()
        except Exception as e:
            log.warning("gamma fetch failed %s: %s", params, e)
            return []

    async def _track_window_markets(self, s: aiohttp.ClientSession, now: int,
                                    kind: str, window_sec: int) -> None:
        """Fixed-window markets: btc-updown-{kind}-{epoch}, current + next."""
        for epoch in (now // window_sec * window_sec,
                      now // window_sec * window_sec + window_sec):
            slug = f"btc-updown-{kind}-{epoch}"
            if slug in self.active or slug in self._known_missing:
                continue
            evs = await self._fetch_event(s, slug=slug)
            if evs:
                mkt = _parse_market(evs[0], kind, kind, epoch, epoch + window_sec)
                if mkt:
                    self.active[slug] = mkt
                    log.info("tracking %s (%s)", mkt.title, slug)
            else:
                self._known_missing.add(slug)

    async def refresh(self) -> None:
        now = int(time.time())
        async with aiohttp.ClientSession() as s:
            if self.enable_5m:
                await self._track_window_markets(s, now, "5m", 300)
            if self.enable_15m:
                await self._track_window_markets(s, now, "15m", 900)
            if self.enable_4h:
                # btc-updown-4h-{epoch}: epochs align with Binance 4h candle
                # boundaries (UTC midnight + 4h multiples), so kind doubles as
                # the kline interval like the other window markets
                await self._track_window_markets(s, now, "4h", 14400)
            if self.enable_hourly:
                evs = await self._fetch_event(
                    s, series_id=HOURLY_SERIES_ID, active="true", closed="false",
                    order="endDate", ascending="true", limit="6",
                )
                for e in evs:
                    slug = e.get("slug", "")
                    if slug in self.active:
                        continue
                    end = e.get("endDate")
                    if not end:
                        continue
                    close_ts = int(time.mktime(time.strptime(end, "%Y-%m-%dT%H:%M:%SZ")) - time.timezone)
                    if close_ts < now or close_ts > now + 3700:
                        continue  # stale or not the live candle yet
                    mkt = _parse_market(e, "1h", "1h", close_ts - 3600, close_ts)
                    if mkt:
                        self.active[slug] = mkt
                        log.info("tracking %s (%s)", mkt.title, slug)

        # refresh live accepting_orders status from the CLOB
        async with aiohttp.ClientSession() as s:
            for mkt in list(self.active.values()):
                try:
                    async with s.get(f"{CLOB}/markets/{mkt.condition_id}",
                                     timeout=aiohttp.ClientTimeout(total=5)) as r:
                        d = await r.json()
                    was = mkt.accepting
                    mkt.accepting = bool(d.get("accepting_orders", True))
                    if was and not mkt.accepting:
                        log.info("market stopped accepting orders: %s", mkt.title)
                except Exception:
                    pass  # keep last known status

        # move expired markets out
        for slug in list(self.active):
            if self.active[slug].t_remaining < -2:
                self.expired.append(self.active.pop(slug))

    async def run(self, interval_sec: float = 5.0) -> None:
        while True:
            try:
                await self.refresh()
            except Exception as e:
                log.exception("market refresh error: %s", e)
            await asyncio.sleep(interval_sec)
