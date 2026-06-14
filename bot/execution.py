"""Order execution: paper simulator (default) and live CLOB executor."""

import asyncio
import itertools
import json
import logging
import os
import random
import time
from collections import deque
from dataclasses import dataclass, field

import websockets

from .markets import Market
from .orderbook import OrderBookFeed

log = logging.getLogger("exec")

MIN_SHARES = 5.0  # Polymarket minimum order size


class FakStats:
    """Track fill-and-kill outcomes with a rolling window for race-loss monitoring."""

    def __init__(self, min_fill_rate: float = 0.50, min_attempts: int = 10,
                 window_size: int = 30):
        self.min_fill_rate = min_fill_rate
        self.min_attempts = min_attempts
        self.window_size = window_size
        self.attempts = 0
        self.fills = 0
        self.kills = 0
        self._recent: deque[bool] = deque(maxlen=window_size)

    def record_fill(self) -> None:
        self.fills += 1
        self._recent.append(True)

    def record_kill(self) -> None:
        self.kills += 1
        self._recent.append(False)

    def record_attempt(self) -> None:
        self.attempts += 1

    def session_fill_rate(self) -> float | None:
        if not self.attempts:
            return None
        return self.fills / self.attempts

    def rolling_fill_rate(self) -> float | None:
        if len(self._recent) < self.min_attempts:
            return None
        return sum(self._recent) / len(self._recent)

    def fill_rate(self) -> float | None:
        """Primary rate for monitoring: rolling window when warm, else session."""
        return self.rolling_fill_rate() or self.session_fill_rate()

    def should_pause(self) -> bool:
        rate = self.rolling_fill_rate()
        return rate is not None and rate < self.min_fill_rate

    def should_resume(self) -> bool:
        rate = self.rolling_fill_rate()
        return rate is not None and rate >= self.min_fill_rate

    @property
    def recent_count(self) -> int:
        return len(self._recent)

    @property
    def recent_fills(self) -> int:
        return sum(self._recent)

    def summary_lines(self) -> list[str]:
        if not self.attempts:
            return []
        sess = 100 * self.fills / self.attempts
        roll = self.rolling_fill_rate()
        roll_str = (f", rolling {100 * roll:.0f}% over last {len(self._recent)}"
                    if roll is not None else "")
        return [f"  FAK orders: {self.fills}/{self.attempts} filled "
                f"({sess:.0f}% session{roll_str}, {self.kills} killed)"]
USER_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/user"


@dataclass
class Position:
    up: float = 0.0
    dn: float = 0.0
    cost: float = 0.0


@dataclass
class OpenOrder:
    id: str
    market: Market
    outcome: str          # "up" | "dn"
    token: str
    price: float
    shares: float
    leg: str = "mm"       # "mm" | "snipe" | "scalp"
    ahead: float = 0.0    # paper mode: displayed size queued ahead of us
    placed_at: float = field(default_factory=time.time)


class Portfolio:
    LEGS = ("mm", "snipe", "scalp")

    def __init__(self, starting_cash: float, state_file: str | None = None):
        self.start_cash = starting_cash
        self.cash = starting_cash
        self.positions: dict[str, Position] = {}
        # per-leg sub-positions for P&L attribution: slug -> leg -> Position
        self.legpos: dict[str, dict[str, Position]] = {}
        # cumulative realized cash flow per strategy leg (converges to true
        # P&L as positions settle)
        self.leg_realized: dict[str, float] = {leg: 0.0 for leg in self.LEGS}
        # session stats
        self.maker_volume = 0.0
        self.taker_volume = 0.0
        self.fees_paid = 0.0
        self.fill_count = 0
        # minimal market metadata per open position, so positions survive restarts
        self.meta: dict[str, dict] = {}
        self.state_file = state_file
        # each hook returns extra lines for log_summary (markouts, paper stats)
        self.summary_hooks: list = []

    def pos(self, slug: str) -> Position:
        return self.positions.setdefault(slug, Position())

    # called with (market, pairs) whenever complete sets become mergeable
    merge_hook = None
    # called with (market, outcome, price, shares, taker, leg) on every fill
    fill_hook = None

    def on_fill(self, market: Market, outcome: str, price: float, shares: float,
                taker: bool = False, leg: str = "mm") -> None:
        p = self.pos(market.slug)
        lp = self.legpos.setdefault(market.slug, {}).setdefault(leg, Position())
        self.meta[market.slug] = {
            "slug": market.slug, "title": market.title,
            "condition_id": market.condition_id,
            "token_up": market.token_up, "token_down": market.token_down,
            "open_ts": market.open_ts, "close_ts": market.close_ts,
            "tick": market.tick, "kind": market.kind, "interval": market.interval,
        }
        if outcome == "up":
            p.up += shares
            lp.up += shares
        else:
            p.dn += shares
            lp.dn += shares
        cost = price * shares
        fee = market.taker_fee_per_share(price) * shares if taker else 0.0
        p.cost += cost
        lp.cost += cost
        self.cash -= cost + fee
        self.leg_realized[leg] -= cost + fee
        self.fees_paid += fee
        self.fill_count += 1
        if taker:
            self.taker_volume += cost
        else:
            self.maker_volume += cost
        # merge complete sets back into cash immediately (worth exactly $1/pair)
        pairs = min(p.up, p.dn)
        if pairs > 0:
            p.up -= pairs
            p.dn -= pairs
            p.cost -= pairs  # pairs redeemed at $1.00 each
            self.cash += pairs
            if self.merge_hook:
                self.merge_hook(market, pairs)
        # intra-leg pair merges count toward that leg's realized flow
        leg_pairs = min(lp.up, lp.dn)
        if leg_pairs > 0:
            lp.up -= leg_pairs
            lp.dn -= leg_pairs
            lp.cost -= leg_pairs
            self.leg_realized[leg] += leg_pairs
        log.info("FILL %-4s %-5s %s %.0f sh @ %.3f ($%.2f%s) | cash %.2f",
                 outcome.upper(), leg, market.title, shares, price, cost,
                 f" +fee {fee:.3f}" if fee else "", self.cash)
        if self.fill_hook:
            self.fill_hook(market, outcome, price, shares, taker, leg)
        self.save()

    def settle(self, market: Market, up_won: bool) -> float:
        p = self.positions.pop(market.slug, None)
        legs = self.legpos.pop(market.slug, {})
        self.meta.pop(market.slug, None)
        if p is None or (p.up == 0 and p.dn == 0):
            self.save()
            return 0.0
        payout = p.up if up_won else p.dn
        self.cash += payout
        pnl = payout - p.cost
        for leg, lp in legs.items():
            self.leg_realized[leg] = self.leg_realized.get(leg, 0.0) + (lp.up if up_won else lp.dn)
        log.info("SETTLE %s -> %s | payout $%.2f cost $%.2f pnl $%+.2f",
                 market.title, "UP" if up_won else "DOWN", payout, p.cost, pnl)
        self.save()
        return pnl

    def log_summary(self) -> None:
        realized = self.cash - self.start_cash
        open_cost = self.exposure()
        log.info("=" * 60)
        log.info("SESSION SUMMARY")
        log.info("  cash $%.2f (started $%.2f) | realized %+.2f | open cost basis $%.2f",
                 self.cash, self.start_cash, realized, open_cost)
        log.info("  fills %d | maker volume $%.2f | taker volume $%.2f | fees paid $%.2f",
                 self.fill_count, self.maker_volume, self.taker_volume, self.fees_paid)
        for leg in self.LEGS:
            if self.leg_realized.get(leg):
                log.info("  leg %-6s realized cash flow %+.2f", leg, self.leg_realized[leg])
        log.info("  (leg flows converge to true per-leg P&L once positions settle;")
        log.info("   maker volume also accrues Polymarket rebates, paid daily off-platform)")
        for hook in self.summary_hooks:
            for line in hook():
                log.info(line)
        log.info("=" * 60)

    # ---------- persistence ----------

    def save(self) -> None:
        if not self.state_file:
            return
        state = {
            "start_cash": self.start_cash,
            "cash": self.cash,
            "positions": {s: {"up": p.up, "dn": p.dn, "cost": p.cost}
                          for s, p in self.positions.items()},
            "legpos": {s: {leg: {"up": lp.up, "dn": lp.dn, "cost": lp.cost}
                           for leg, lp in legs.items()}
                       for s, legs in self.legpos.items()},
            "leg_realized": self.leg_realized,
            "meta": self.meta,
        }
        tmp = self.state_file + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f, indent=1)
        os.replace(tmp, self.state_file)

    def restore(self) -> list[Market]:
        """Load saved state. Returns Market stubs for restored positions whose
        windows already closed, so they can be settled."""
        if not self.state_file or not os.path.exists(self.state_file):
            return []
        with open(self.state_file) as f:
            state = json.load(f)
        self.start_cash = state["start_cash"]
        self.cash = state["cash"]
        self.positions = {s: Position(**p) for s, p in state["positions"].items()}
        self.legpos = {s: {leg: Position(**lp) for leg, lp in legs.items()}
                       for s, legs in state.get("legpos", {}).items()}
        self.leg_realized = {**{leg: 0.0 for leg in self.LEGS},
                             **state.get("leg_realized", {})}
        self.meta = state.get("meta", {})
        stubs = []
        for slug, md in self.meta.items():
            if slug in self.positions and md["close_ts"] < time.time():
                stubs.append(Market(
                    slug=md["slug"], title=md["title"], condition_id=md["condition_id"],
                    token_up=md["token_up"], token_down=md["token_down"],
                    open_ts=md["open_ts"], close_ts=md["close_ts"],
                    tick=md["tick"], kind=md["kind"], interval=md["interval"],
                ))
        log.info("restored state: cash $%.2f, %d open positions (%d awaiting settlement)",
                 self.cash, len(self.positions), len(stubs))
        return stubs

    def exposure(self) -> float:
        return sum(p.cost for p in self.positions.values())

    def equity(self, marks: dict[str, float]) -> float:
        """marks: slug -> fair P(up)."""
        v = self.cash
        for slug, p in self.positions.items():
            fair = marks.get(slug, 0.5)
            v += p.up * fair + p.dn * (1 - fair)
        return v


class PaperExecutor:
    """Simulates fills against the live order book with queue-position modeling
    and order round-trip latency.

    Taker (snipe/scalp, FAK): the order "arrives" after a simulated latency and
    only fills if the ask still exists at our price *then*, with whatever size
    is displayed then. Sniping stale quotes is a latency race — instant fills
    against our own book snapshot would credit us a 100% win rate on exactly
    the fleeting quotes that faster bots (or the owner's cancel) take in
    reality.
    Maker: when we place a resting bid, the size already displayed at that
    price level is queued ahead of us. Trades through our level (strictly
    lower price) fill us fully; trades *at* our level first burn through the
    queue ahead, then fill us partially with whatever size remains. Cancels
    take effect after the same latency, so quotes stay hittable during a jump
    just like live cancels in flight.
    """

    EPS = 1e-9

    def __init__(self, portfolio: Portfolio, feed: OrderBookFeed,
                 taker_latency_ms: float = 350.0, cancel_latency_ms: float = 150.0,
                 fak_min_fill_rate: float = 0.50, fak_min_attempts: int = 10,
                 fak_window_size: int = 30):
        self.portfolio = portfolio
        self.feed = feed
        self.taker_latency = taker_latency_ms / 1000.0
        self.cancel_latency = cancel_latency_ms / 1000.0
        self.open_orders: dict[str, OpenOrder] = {}
        self.fak_stats = FakStats(fak_min_fill_rate, fak_min_attempts, fak_window_size)
        self._ids = itertools.count(1)
        feed.on_trade.append(self._on_trade)
        portfolio.summary_hooks.append(self.fak_stats.summary_lines)

    def _fill(self, oid: str, o: OpenOrder, shares: float) -> None:
        o.shares -= shares
        if o.shares < MIN_SHARES:  # remainder too small to matter
            self.open_orders.pop(oid, None)
        self.portfolio.on_fill(o.market, o.outcome, o.price, shares, leg=o.leg)

    def _on_trade(self, asset: str, price: float, size: float) -> None:
        for oid in list(self.open_orders):
            o = self.open_orders[oid]
            if o.token != asset:
                continue
            if price < o.price - self.EPS:
                # traded through our level: assume we were swept
                self._fill(oid, o, o.shares)
            elif price <= o.price + self.EPS:
                # traded at our level: queue ahead of us absorbs first
                take = min(size, o.ahead)
                o.ahead -= take
                rem = size - take
                if rem > 0:
                    self._fill(oid, o, min(rem, o.shares))

    async def _delayed_fak(self, market: Market, outcome: str, token: str,
                           price: float, shares: float, leg: str) -> None:
        """Taker order arrives at the book after a simulated round trip; fill
        only with whatever is still offered at our price when it gets there."""
        self.fak_stats.record_attempt()
        await asyncio.sleep(self.taker_latency * random.uniform(0.5, 1.5))
        if market.close_ts <= time.time():
            self.fak_stats.record_kill()
            return  # window closed while the order was in flight
        book = self.feed.books.get(token)
        ask = book.best_ask() if book else None
        if not ask or ask[0] > price + self.EPS or ask[1] <= 0:
            self.fak_stats.record_kill()
            log.info("paper FAK killed: %s %s, ask gone from %.3f (lost the race)",
                     market.title, outcome.upper(), price)
            return
        self.fak_stats.record_fill()
        self.portfolio.on_fill(market, outcome, ask[0], min(shares, ask[1]),
                               taker=True, leg=leg)

    async def place_buy(self, market: Market, outcome: str, price: float, shares: float,
                        leg: str = "mm") -> str | None:
        if shares < MIN_SHARES:
            return None
        token = market.token_up if outcome == "up" else market.token_down
        if leg != "mm":
            # taker legs are fill-and-kill, raced against everyone else who
            # sees the same stale quote: the fill resolves after the simulated
            # latency against whatever the book looks like then
            asyncio.ensure_future(
                self._delayed_fak(market, outcome, token, price, shares, leg))
            return None
        book = self.feed.books.get(token)
        ask = book.best_ask() if book else None
        if ask and ask[0] <= price:
            # MM quote would cross (post-only clamp makes this rare): fill as
            # taker against the displayed size only
            self.portfolio.on_fill(market, outcome, ask[0], min(shares, ask[1]),
                                   taker=True, leg=leg)
            return None
        ahead = book.bids.get(price, 0.0) if book else 0.0
        oid = f"paper-{next(self._ids)}"
        self.open_orders[oid] = OpenOrder(oid, market, outcome, token, price, shares,
                                          leg=leg, ahead=ahead)
        return oid

    async def cancel(self, order_id: str) -> None:
        # a live cancel takes a round trip too: the quote stays hittable while
        # the cancel is in flight (this is when jump-guard pulls get picked off)
        if self.cancel_latency > 0 and order_id in self.open_orders:
            asyncio.get_running_loop().call_later(
                self.cancel_latency, lambda: self.open_orders.pop(order_id, None))
        else:
            self.open_orders.pop(order_id, None)

    async def cancel_market(self, market: Market) -> None:
        for oid in list(self.open_orders):
            if self.open_orders[oid].market.slug == market.slug:
                del self.open_orders[oid]


class LiveExecutor:
    """Real orders through the Polymarket CLOB via py-clob-client."""

    def __init__(self, portfolio: Portfolio, host: str, chain_id: int,
                 private_key: str, funder: str, signature_type: int,
                 onchain=None, fak_min_fill_rate: float = 0.50,
                 fak_min_attempts: int = 10, fak_window_size: int = 30):
        from py_clob_client.client import ClobClient

        self.portfolio = portfolio
        self.client = ClobClient(
            host, key=private_key, chain_id=chain_id,
            signature_type=signature_type, funder=funder,
        )
        self.client.set_api_creds(self.client.create_or_derive_api_creds())
        self.open_orders: dict[str, OpenOrder] = {}
        self.fak_stats = FakStats(fak_min_fill_rate, fak_min_attempts, fak_window_size)
        self._fak_filled: dict[str, bool] = {}  # order_id -> got any taker fill
        self._seen_trades: set[str] = set()
        self.onchain = onchain
        self._merge_queue: dict[str, Market] = {}   # condition_id -> market
        self._redeem_queue: dict[str, Market] = {}
        self._tracked_conditions: set[str] = set()
        self._user_reconnect = asyncio.Event()
        if onchain is not None:
            portfolio.merge_hook = lambda market, pairs: self._merge_queue.setdefault(
                market.condition_id, market)
        portfolio.summary_hooks.append(self.fak_stats.summary_lines)
        log.info("live executor ready (funder %s...)", funder[:10])

    def track_markets(self, condition_ids: set[str]) -> None:
        """Keep the user websocket subscribed to all markets we trade."""
        condition_ids = {c for c in condition_ids if c}
        if condition_ids != self._tracked_conditions:
            self._tracked_conditions = condition_ids
            self._user_reconnect.set()

    def queue_redeem(self, market: Market) -> None:
        if self.onchain is not None and market.condition_id:
            self._redeem_queue[market.condition_id] = market

    async def process_onchain(self) -> None:
        """Background loop: merge complete sets, redeem resolved markets."""
        while True:
            await asyncio.sleep(20)
            if self.onchain is None:
                continue
            for cid, m in list(self._merge_queue.items()):
                try:
                    await self.onchain.merge(cid, m.token_up, m.token_down)
                    del self._merge_queue[cid]
                except Exception as e:
                    log.warning("merge failed for %s: %s", m.title, e)
            for cid, m in list(self._redeem_queue.items()):
                try:
                    if await self.onchain.redeem(cid):  # False until oracle resolves
                        del self._redeem_queue[cid]
                except Exception as e:
                    log.warning("redeem failed for %s: %s", m.title, e)

    async def place_buy(self, market: Market, outcome: str, price: float, shares: float,
                        leg: str = "mm") -> str | None:
        from py_clob_client.clob_types import OrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY

        if shares < MIN_SHARES:
            return None
        token = market.token_up if outcome == "up" else market.token_down
        args = OrderArgs(price=round(price, 3), size=round(shares, 2), side=BUY, token_id=token)
        # taker legs are fill-and-kill: take what's available at our price,
        # never leave a remainder resting on the book as a stale quote for
        # someone else to pick off. Only MM quotes rest (GTC).
        order_type = OrderType.GTC if leg == "mm" else OrderType.FAK
        try:
            signed = await asyncio.to_thread(self.client.create_order, args)
            resp = await asyncio.to_thread(self.client.post_order, signed, order_type)
        except Exception as e:
            log.warning("order rejected: %s", e)
            return None
        oid = resp.get("orderID")
        if oid:
            self.open_orders[oid] = OpenOrder(oid, market, outcome, token, price, shares, leg=leg)
            if order_type != OrderType.GTC:
                self.fak_stats.record_attempt()
                self._fak_filled[oid] = False
                # FAK orders never rest; keep the entry just long enough for
                # the user-feed trade event to attribute the fill, then drop it
                asyncio.get_running_loop().call_later(
                    30.0, lambda o=oid: self._resolve_fak(o))
        return oid

    def _resolve_fak(self, order_id: str) -> None:
        """Count zero-fill FAK orders as race losses once the fill window closes."""
        o = self.open_orders.pop(order_id, None)
        filled = self._fak_filled.pop(order_id, False)
        if o is None or filled:
            return
        self.fak_stats.record_kill()
        log.info("live FAK killed: %s %s @ %.3f (lost the race)",
                 o.market.title, o.outcome.upper(), o.price)

    async def cancel(self, order_id: str) -> None:
        try:
            await asyncio.to_thread(self.client.cancel, order_id)
        except Exception as e:
            log.warning("cancel failed %s: %s", order_id, e)
        self.open_orders.pop(order_id, None)

    async def cancel_market(self, market: Market) -> None:
        for oid in list(self.open_orders):
            if self.open_orders[oid].market.slug == market.slug:
                await self.cancel(oid)

    # ---------- fill tracking via the CLOB user websocket ----------

    def _apply_fill(self, oid: str, price: float, size: float, taker: bool) -> None:
        o = self.open_orders.get(oid)
        if o is None or size <= 0:
            return
        fill = min(size, o.shares)
        o.shares -= fill
        if o.shares <= 1e-9:
            self.open_orders.pop(oid, None)
        self.portfolio.on_fill(o.market, o.outcome, price, fill, taker=taker, leg=o.leg)

    def _handle_user_msg(self, d: dict) -> None:
        et = d.get("event_type")
        if et == "trade":
            tid = d.get("id")
            if not tid or tid in self._seen_trades:
                return  # trades emit repeated status updates (MATCHED/MINED/...)
            self._seen_trades.add(tid)
            # we may be the taker...
            taker_oid = d.get("taker_order_id")
            if taker_oid in self.open_orders:
                if not self._fak_filled.get(taker_oid):
                    self._fak_filled[taker_oid] = True
                    self.fak_stats.record_fill()
                price = float(d.get("price", 0))
                size = float(d.get("size", 0))
                # FEE VERIFICATION: docs say fee = 0.07*p*(1-p)/share (what the
                # bot models); a third-party bot's live fills matched the old
                # 0.07*min(p,1-p) formula instead. Log what the exchange
                # actually reports so the first live fill settles the question
                # (research/REPORT.md addendum). Remove once confirmed.
                fee_fields = {k: v for k, v in d.items() if "fee" in k.lower()}
                if price > 0 and size > 0:
                    log.info("FEE CHECK trade %s: price=%.3f size=%.2f | exchange fee "
                             "fields=%s | formula p(1-p)=%.4f/sh min(p,1-p)=%.4f/sh",
                             tid, price, size, fee_fields or "(none reported)",
                             0.07 * price * (1 - price),
                             0.07 * min(price, 1 - price))
                self._apply_fill(taker_oid, price, size, taker=True)
            # ...and/or one of the makers (most fills for this strategy)
            for mo in d.get("maker_orders", []):
                oid = mo.get("order_id")
                if oid in self.open_orders:
                    price = float(mo.get("price") or d.get("price", 0))
                    self._apply_fill(oid, price,
                                     float(mo.get("matched_amount", 0)), taker=False)
        elif et == "order":
            status = (d.get("status") or "").upper()
            oid = d.get("id")
            if status in ("CANCELED", "CANCELLED") and oid in self.open_orders:
                self.open_orders.pop(oid, None)

    async def run_user_feed(self) -> None:
        """Stream our own orders/trades; replaces fill polling entirely."""
        creds = self.client.creds
        auth = {"apiKey": creds.api_key, "secret": creds.api_secret,
                "passphrase": creds.api_passphrase}
        while True:
            if not self._tracked_conditions:
                await asyncio.sleep(1)
                continue
            self._user_reconnect.clear()
            markets = sorted(self._tracked_conditions)
            try:
                async with websockets.connect(USER_WS_URL, ping_interval=None) as ws:
                    await ws.send(json.dumps({"type": "user", "markets": markets, "auth": auth}))
                    log.info("user feed subscribed to %d markets", len(markets))

                    async def pinger():
                        while True:
                            await asyncio.sleep(10)
                            await ws.send("PING")

                    ping_task = asyncio.create_task(pinger())
                    try:
                        while not self._user_reconnect.is_set():
                            try:
                                msg = await asyncio.wait_for(ws.recv(), timeout=1.0)
                            except asyncio.TimeoutError:
                                continue
                            if msg == "PONG":
                                continue
                            data = json.loads(msg)
                            for item in data if isinstance(data, list) else [data]:
                                self._handle_user_msg(item)
                    finally:
                        ping_task.cancel()
            except Exception as e:
                log.warning("user ws error: %s; reconnecting in 2s", e)
                await asyncio.sleep(2)
