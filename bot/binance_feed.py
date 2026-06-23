"""Live Binance price feed + realized volatility estimator + candle opens.

Subscribes to both the trade stream (for realized vol) and bookTicker (for a
faster, sharper price signal: the top-of-book mid updates between trades).
Volatility is estimated on two horizons — a fast EWMA that reacts to bursts
and a slow EWMA that anchors the baseline — and the *larger* of the two is
used, which keeps fair-value tails conservative when the regime shifts.

Optionally also subscribes to the USDT-M perpetual futures bookTicker. Perps
lead spot by milliseconds-to-seconds in fast moves (price discovery happens
on the perp), so when the perp print is fresher than the spot print, the spot
estimate becomes (perp mid − rolling perp-spot basis). Markets still resolve
on spot/Chainlink, so the perp is only ever a faster *estimator of spot* —
never the fair-value anchor itself.

When a CoinbaseFeed is attached, Coinbase BBO is blended in *basis-adjusted*:
(coinbase mid − rolling coinbase-binance basis), exactly like the perp. The
composite therefore stays in the Binance price frame — the same frame as the
candle open (kline_open) and paper settlement (kline_close) — while still
picking up Coinbase's independent price information. Blending raw Coinbase
levels is a trap: Coinbase routinely trades $50-100 off Binance, and that
offset becomes a permanent directional skew in P(up) since the open is a
Binance price (session 1781182217 lost ~$660 buying DOWN all night because
of exactly this).

The fstream connection can also carry @forceOrder (liquidations). Significant
liquidation prints fire on_liquidation callbacks so the jump guard can pull
MM quotes before the cascade hits spot.
"""

import asyncio
import json
import logging
import math
import time
from collections import deque
from typing import Callable

import aiohttp
import websockets

log = logging.getLogger("binance")

# Only escalate to WARNING after this many consecutive reconnect failures; a
# routine idle-close that recovers on the next attempt stays at DEBUG.
WS_WARN_AFTER = 3

# binance.vision hosts are Binance's public market-data mirror (no geo-block)
WS_URL = "wss://data-stream.binance.vision/stream?streams={streams}"
REST_KLINES = "https://data-api.binance.vision/api/v3/klines"
# USDT-M perpetual futures (no public mirror; geo-restricted in some regions)
FSTREAM_WS_URL = "wss://fstream.binance.com/stream?streams={streams}"

LiquidationCb = Callable[[str, float, float, float, float], None]


class BinanceFeed:
    SAMPLE_SEC = 1.0   # sample returns at 1s to avoid microstructure noise
    MID_FRESH_SEC = 2.0
    BASIS_FRESH_SEC = 30.0  # never trust a perp-implied spot on a stale basis

    def __init__(self, symbol: str, vol_halflife_fast_sec: float = 60,
                 vol_halflife_slow_sec: float = 600, min_vol_per_sec: float = 2e-5,
                 perp_symbol: str | None = None, basis_halflife_sec: float = 120,
                 coinbase=None, coinbase_weight: float = 0.5):
        self.symbol = symbol.lower()
        self.trade_price: float | None = None
        self.mid_price: float | None = None
        self.mid_ts: float = 0.0
        self.last_ts: float | None = None     # last trade event time (exchange clock)
        self.last_local_ts: float = 0.0       # last message time (local clock)
        self._hl_fast = vol_halflife_fast_sec
        self._hl_slow = vol_halflife_slow_sec
        self._var_fast: float | None = None
        self._var_slow: float | None = None
        self._min_vol = min_vol_per_sec
        self._sample_price: float | None = None
        self._sample_ts: float | None = None
        self._vol_first_ts: float | None = None  # first variance sample (warmup tracking)
        # rolling 1s price samples for short-horizon momentum (drift) estimates
        self._mom_samples: deque[tuple[float, float]] = deque()
        self._mom_max_window = 300.0
        self._kline_cache: dict[tuple[str, int], float] = {}
        # perp futures lead signal
        self.perp_symbol = perp_symbol.lower() if perp_symbol else None
        self.perp_mid: float | None = None
        self.perp_ts: float = 0.0
        self._basis: float | None = None      # EWMA of (perp mid − composite spot)
        self._basis_ts: float = 0.0
        self._basis_hl = basis_halflife_sec
        # coinbase composite spot (basis-adjusted into the Binance frame)
        self._coinbase = coinbase
        self._coinbase_weight = max(0.0, min(1.0, coinbase_weight))
        self._cb_basis: float | None = None   # EWMA of (coinbase mid − binance mid)
        self._cb_basis_ts: float = 0.0
        self._cb_basis_hl = basis_halflife_sec
        # liquidations on fstream
        self._liq_enabled = False
        self._liq_min_notional = 50_000.0
        self.on_liquidation: list[LiquidationCb] = []

    def configure_liquidations(self, min_notional_usd: float = 50_000.0) -> None:
        self._liq_enabled = True
        self._liq_min_notional = min_notional_usd

    @property
    def perp_basis(self) -> float | None:
        return self._basis

    @property
    def coinbase_basis(self) -> float | None:
        """Coinbase mid minus Binance mid (positive = Coinbase higher)."""
        if (self._coinbase and self.mid_price is not None
                and self._coinbase.mid_price is not None):
            return self._coinbase.mid_price - self.mid_price
        return None

    def _update_cb_basis(self, now: float) -> None:
        """EWMA of (coinbase mid − binance mid), sampled on binance ticks."""
        cb = self._coinbase
        if (cb is None or cb.mid_price is None or self.mid_price is None
                or now - cb.mid_ts > self.MID_FRESH_SEC):
            return
        sample = cb.mid_price - self.mid_price
        if self._cb_basis is None:
            self._cb_basis = sample
        else:
            dt = max(now - self._cb_basis_ts, 1e-3)
            alpha = 1 - 0.5 ** (dt / self._cb_basis_hl)
            self._cb_basis += alpha * (sample - self._cb_basis)
        self._cb_basis_ts = now

    def _composite_spot(self, now: float) -> tuple[float | None, float]:
        """Weighted mid from fresh spot venues, in the *Binance* price frame;
        returns (price, freshest_ts).

        Coinbase enters basis-adjusted (mid − rolling basis) so its level
        offset vs Binance never leaks into fair value — the candle open and
        settlement are Binance prices, so the model must be too. Until a
        basis estimate exists (both feeds ticking), Coinbase is excluded
        rather than blended raw.
        """
        parts: list[tuple[float, float, float]] = []
        if self.mid_price is not None and now - self.mid_ts < self.MID_FRESH_SEC:
            w = 1.0 - (self._coinbase_weight if self._coinbase else 0.0)
            parts.append((self.mid_price, self.mid_ts, w))
        if self._coinbase and self._cb_basis is not None \
                and now - self._cb_basis_ts < self.BASIS_FRESH_SEC:
            cb = self._coinbase
            if cb.mid_price is not None and now - cb.mid_ts < self.MID_FRESH_SEC:
                parts.append((cb.mid_price - self._cb_basis, cb.mid_ts,
                              self._coinbase_weight))
        if not parts:
            return None, 0.0
        total_w = sum(p[2] for p in parts)
        price = sum(p[0] * p[2] for p in parts) / total_w
        return price, max(p[1] for p in parts)

    @property
    def price(self) -> float | None:
        """Freshest available estimate of spot.

        Perps lead spot in fast moves, so when the perp print is newer than
        the composite spot print, (perp mid − rolling basis) is the sharper
        estimate. Falls back to composite spot mid, then last trade.
        """
        now = time.time()
        composite, comp_ts = self._composite_spot(now)
        spot_fresh = composite is not None
        perp_fresh = (self.perp_mid is not None and self._basis is not None
                      and now - self.perp_ts < self.MID_FRESH_SEC
                      and now - self._basis_ts < self.BASIS_FRESH_SEC)
        if perp_fresh and (not spot_fresh or self.perp_ts >= comp_ts):
            return self.perp_mid - self._basis
        if spot_fresh:
            return composite
        return self.trade_price

    @property
    def feed_age(self) -> float:
        return time.time() - self.last_local_ts if self.last_local_ts else float("inf")

    @property
    def vol_per_sec(self) -> float:
        candidates = [v for v in (self._var_fast, self._var_slow) if v is not None]
        if not candidates:
            return self._min_vol
        return max(math.sqrt(max(candidates)), self._min_vol)

    @property
    def vol_age(self) -> float:
        """Seconds since the first variance sample. The EWMA needs roughly a
        fast-halflife of data before vol_per_sec means anything; before that
        it sits at the min_vol floor and the fair-value model is massively
        overconfident (session 1781248550 lost $70 sniping both sides of one
        window 9 seconds after startup, when a $20 wobble looked like 1 sigma)."""
        if self._vol_first_ts is None or self.last_ts is None:
            return 0.0
        return max(0.0, self.last_ts - self._vol_first_ts)

    def _update_vol(self, price: float, ts: float) -> None:
        if self._sample_price is None or self._sample_ts is None:
            self._sample_price, self._sample_ts = price, ts
            return
        dt = ts - self._sample_ts
        if dt < self.SAMPLE_SEC:
            return
        r = math.log(price / self._sample_price)
        sample = (r * r) / dt  # variance per second
        if self._vol_first_ts is None:
            self._vol_first_ts = ts
        for attr, hl in (("_var_fast", self._hl_fast), ("_var_slow", self._hl_slow)):
            cur = getattr(self, attr)
            alpha = 1 - 0.5 ** (dt / hl)
            setattr(self, attr, sample if cur is None else cur + alpha * (sample - cur))
        self._sample_price, self._sample_ts = price, ts
        self._mom_samples.append((ts, price))
        cutoff = ts - self._mom_max_window
        while self._mom_samples and self._mom_samples[0][0] < cutoff:
            self._mom_samples.popleft()

    def recent_return(self, window_sec: float) -> float | None:
        """Log return over (approximately) the trailing window_sec.

        Returns None until at least half the window is covered by samples,
        so a freshly (re)connected feed never fabricates momentum.
        """
        if not self._mom_samples:
            return None
        now_ts, now_px = self._mom_samples[-1]
        target = now_ts - window_sec
        base = None
        for ts, px in self._mom_samples:
            if ts >= target:
                base = (ts, px)
                break
        if base is None or now_ts - base[0] < window_sec * 0.5 or base[1] <= 0:
            return None
        return math.log(now_px / base[1])

    def _update_perp_basis(self, now: float) -> None:
        composite, _ = self._composite_spot(now)
        if composite is None:
            return
        sample = self.perp_mid - composite
        if self._basis is None:
            self._basis = sample
        else:
            dt = max(now - self._basis_ts, 1e-3)
            alpha = 1 - 0.5 ** (dt / self._basis_hl)
            self._basis += alpha * (sample - self._basis)
        self._basis_ts = now

    def _handle_liquidation(self, data: dict) -> None:
        o = data.get("o") or {}
        side = o.get("S", "")
        try:
            qty = float(o.get("q") or o.get("l") or 0)
            px = float(o.get("ap") or o.get("p") or 0)
        except (TypeError, ValueError):
            return
        if qty <= 0 or px <= 0:
            return
        notional = qty * px
        if notional < self._liq_min_notional:
            return
        now = time.time()
        log.info("liquidation %s $%.0f @ %.1f (%.4f BTC)", side, notional, px, qty)
        for cb in self.on_liquidation:
            cb(side, qty, px, notional, now)

    async def run(self) -> None:
        streams = f"{self.symbol}@trade/{self.symbol}@bookTicker"
        url = WS_URL.format(streams=streams)
        connected_once = False
        fails = 0
        while True:
            try:
                async with websockets.connect(url, ping_interval=20) as ws:
                    if fails >= WS_WARN_AFTER:
                        log.info("binance %s reconnected", streams)
                    elif not connected_once:
                        log.info("connected to binance %s", streams)
                    connected_once = True
                    fails = 0
                    async for msg in ws:
                        d = json.loads(msg)
                        data = d.get("data", {})
                        self.last_local_ts = time.time()
                        if d.get("stream", "").endswith("@trade"):
                            px = float(data["p"])
                            ts = data["T"] / 1000.0
                            self.trade_price = px
                            self.last_ts = ts
                            self._update_vol(px, ts)
                        elif d.get("stream", "").endswith("@bookTicker"):
                            bid, ask = float(data["b"]), float(data["a"])
                            if bid > 0 and ask > 0:
                                self.mid_price = (bid + ask) / 2
                                self.mid_ts = time.time()
                                self._update_cb_basis(self.mid_ts)
            except Exception as e:
                fails += 1
                if fails >= WS_WARN_AFTER:
                    log.warning("binance ws error: %s; reconnecting in 2s (%d consecutive)", e, fails)
                else:
                    log.debug("binance ws error: %s; reconnecting in 2s", e)
                await asyncio.sleep(2)

    async def run_fstream(self) -> None:
        """USDT-M fstream: perp bookTicker + optional forceOrder liquidations.

        Purely additive: if this feed is down or geo-blocked, `price` falls
        back to the spot feed and the bot trades exactly as before.
        """
        if not self.perp_symbol and not self._liq_enabled:
            return
        streams: list[str] = []
        if self.perp_symbol:
            streams.append(f"{self.perp_symbol}@bookTicker")
            if self._liq_enabled:
                streams.append(f"{self.perp_symbol}@forceOrder")
        elif self._liq_enabled:
            streams.append(f"{self.symbol}@forceOrder")
        url = FSTREAM_WS_URL.format(streams="/".join(streams))
        backoff = 2.0
        connected_once = False
        fails = 0
        while True:
            try:
                async with websockets.connect(url, ping_interval=20) as ws:
                    if fails >= WS_WARN_AFTER:
                        log.info("binance fstream %s reconnected", "/".join(streams))
                    elif not connected_once:
                        log.info("connected to binance fstream %s", "/".join(streams))
                    connected_once = True
                    fails = 0
                    backoff = 2.0
                    async for msg in ws:
                        payload = json.loads(msg).get("data", {})
                        if payload.get("e") == "forceOrder":
                            self._handle_liquidation(payload)
                            continue
                        bid = float(payload.get("b", 0))
                        ask = float(payload.get("a", 0))
                        if bid <= 0 or ask <= 0:
                            continue
                        now = time.time()
                        self.perp_mid = (bid + ask) / 2
                        self.perp_ts = now
                        self._update_perp_basis(now)
            except Exception as e:
                fails += 1
                if fails >= WS_WARN_AFTER:
                    log.warning("binance fstream ws error: %s; retrying in %.0fs "
                                "(%d consecutive; spot feed still drives trading)",
                                e, backoff, fails)
                else:
                    log.debug("binance fstream ws error: %s; retrying in %.0fs", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def run_perp(self) -> None:
        """Backward-compatible alias for run_fstream()."""
        await self.run_fstream()

    async def kline_open(self, interval: str, open_ts: int) -> float | None:
        """Open price of the candle starting at open_ts (unix seconds)."""
        key = (interval, open_ts)
        if key in self._kline_cache:
            return self._kline_cache[key]
        params = {
            "symbol": self.symbol.upper(),
            "interval": interval,
            "startTime": open_ts * 1000,
            "limit": 1,
        }
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(REST_KLINES, params=params, timeout=aiohttp.ClientTimeout(total=5)) as r:
                    data = await r.json()
            if data and int(data[0][0]) == open_ts * 1000:
                o = float(data[0][1])
                self._kline_cache[key] = o
                return o
        except Exception as e:
            log.warning("kline_open fetch failed: %s", e)
        return None

    async def kline_close(self, interval: str, open_ts: int) -> float | None:
        """Close price of a *finished* candle starting at open_ts."""
        params = {
            "symbol": self.symbol.upper(),
            "interval": interval,
            "startTime": open_ts * 1000,
            "limit": 1,
        }
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(REST_KLINES, params=params, timeout=aiohttp.ClientTimeout(total=5)) as r:
                    data = await r.json()
            if data and int(data[0][0]) == open_ts * 1000:
                close_time_ms = int(data[0][6])
                if close_time_ms < time.time() * 1000:  # candle finished
                    return float(data[0][4])
        except Exception as e:
            log.warning("kline_close fetch failed: %s", e)
        return None
