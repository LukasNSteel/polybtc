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

# Per-write sequence so concurrent state saves never share a temp filename.
_save_counter = itertools.count()

# Only escalate a reconnect to WARNING after this many consecutive failures; a
# routine idle-close that recovers on the next attempt stays at DEBUG.
WS_WARN_AFTER = 3


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
        # paper speed-bump model: fills taken while the side drifted against us
        # during the uncancellable hold (adverse selection the old race model
        # was blind to). adverse_drift sums the |fair move| we ate on those.
        self.adverse_fills = 0
        self.adverse_drift = 0.0
        self._recent: deque[bool] = deque(maxlen=window_size)
        # wall-clock of the last taker attempt; used to expire a stale rolling
        # window so a pause can't deadlock (paused takers log no new outcomes,
        # so the window would otherwise stay frozen below the resume threshold
        # forever — see Strategy._check_fak_monitor).
        self.last_attempt_ts = 0.0

    def record_fill(self) -> None:
        self.fills += 1
        self._recent.append(True)

    def record_adverse(self, drift: float) -> None:
        """A fill taken while the underlying drifted against us during the
        250ms uncancellable hold. drift is the adverse fair-value move (>0)."""
        self.adverse_fills += 1
        self.adverse_drift += drift

    def record_kill(self) -> None:
        self.kills += 1
        self._recent.append(False)

    def record_attempt(self) -> None:
        self.attempts += 1
        self.last_attempt_ts = time.time()

    def decay_if_stale(self, stale_sec: float) -> bool:
        """Clear the rolling window if no taker has been attempted for
        `stale_sec`. Returns True if it cleared. This is the recovery valve for
        the pause deadlock: while takers are paused no outcomes are recorded, so
        the window goes stale and is forgotten, letting the monitor re-probe
        from a clean slate instead of staying frozen on an old bad streak."""
        if stale_sec <= 0 or not self._recent:
            return False
        if self.last_attempt_ts and (time.time() - self.last_attempt_ts) > stale_sec:
            self._recent.clear()
            return True
        return False

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
        lines = [f"  FAK orders: {self.fills}/{self.attempts} filled "
                 f"({sess:.0f}% session{roll_str}, {self.kills} killed)"]
        if self.adverse_fills:
            avg = self.adverse_drift / self.adverse_fills
            lines.append(
                f"    of which {self.adverse_fills} adverse-selection fills "
                f"(filled while fair drifted against us during the 250ms hold, "
                f"avg {avg:.3f} / {100 * avg:.1f}c per share)")
        return lines
USER_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/user"


def _signing_backend() -> tuple[bool, str]:
    """Return (is_native, name) for eth-keys' active secp256k1 backend.
    The native libsecp256k1 backend (coincurve) signs an order in ~0.15ms vs
    ~3.5ms for the pure-Python fallback — a 22x cut that keeps EIP-712 signing
    off the snipe critical path. We surface this at startup so a fallback to
    pure Python (e.g. coincurve missing on the deploy box) is loud, not silent."""
    try:
        from eth_keys.backends import get_default_backend_class
        name = str(get_default_backend_class())
    except Exception as e:  # noqa: BLE001 — never let a probe stop the bot
        return False, f"unknown ({e})"
    return "coincurve" in name.lower(), name.rsplit(".", 1)[-1]


def _parse_fill_shares(resp: dict, requested: float) -> float:
    """Shares filled by a marketable FAK buy, parsed from the post-order
    response. Falls back to `requested` on a MATCHED/FILLED status with no
    explicit amount, and 0 on any error/no-fill."""
    if not isinstance(resp, dict):
        return 0.0
    for key in ("takingAmount", "taking_amount", "size_matched", "sizeMatched"):
        val = resp.get(key)
        if val is not None:
            try:
                return float(val)
            except (TypeError, ValueError):
                pass
    if resp.get("success") is False or resp.get("error"):
        return 0.0
    status = str(resp.get("status") or "").upper()
    if status in ("MATCHED", "FILLED"):
        return requested
    return 0.0


def _parse_fill_price(resp: dict, fallback: float) -> float:
    """Average fill price (collateral spent / shares received) when the
    response reports both legs, else the limit price (a conservative upper
    bound — a FAK buy never fills above its limit)."""
    if isinstance(resp, dict):
        making = resp.get("makingAmount") or resp.get("making_amount")
        taking = resp.get("takingAmount") or resp.get("taking_amount")
        try:
            if making and taking and float(taking) > 0:
                return round(float(making) / float(taking), 4)
        except (TypeError, ValueError):
            pass
    return fallback


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
        # A fixed "state.json.tmp" is a shared path: if two writers (e.g. the
        # strategy task and the live user-ws fill handler, or a second bot
        # instance in the same dir) write it concurrently, the first os.replace
        # consumes the temp and the second finds it gone -> FileNotFoundError,
        # which previously bubbled up and failed the whole strategy step. A
        # pid+seq temp name keeps each write private, and a failed save now
        # degrades to a warning instead of crashing the loop.
        tmp = f"{self.state_file}.{os.getpid()}.{next(_save_counter)}.tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(state, f, indent=1)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.state_file)
        except OSError as e:
            log.warning("state save failed (%s); continuing", e)
            try:
                os.remove(tmp)
            except OSError:
                pass

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
    and the itode speed-bump execution mechanism.

    Taker (snipe/scalp, FAK): models Polymarket's speed bump, NOT a cancellable
    latency race. On submission the order is committed for the full tick-to-
    trade budget (taker_latency); the last speed_bump portion is a FROZEN,
    UNCANCELLABLE hold. We re-validate against the book only at the END of the
    hold:
      * if the side richened during the hold (BTC moved in our favour) the
        cheap quote is gone above our limit -> the FAK is rejected (a costless
        miss; faster takers got the print);
      * if the side cheapened (BTC moved against us) we are committed and get
        filled anyway, at a now-worse fair value -> adverse selection.
    The old model treated every close call as a costless "lost the race" kill,
    which understated adverse selection and credited cancels that do not exist
    live. There is no cancel escape during the hold.
    Maker: when we place a resting bid, the size already displayed at that
    price level is queued ahead of us. Trades through our level (strictly
    lower price) fill us fully; trades *at* our level first burn through the
    queue ahead, then fill us partially with whatever size remains. Cancels
    take effect after cancel_latency (GTC quotes are genuinely cancellable,
    unlike the marketable taker orders above).
    """

    EPS = 1e-9

    # how far below fair (book mid) a quote must sit to count as "fully
    # contested" — at this discount every latency arb races it, so our capture
    # of the displayed size goes to ~0. 0.10 = 10c below mid.
    CONTENTION_SCALE = 0.10

    def __init__(self, portfolio: Portfolio, feed: OrderBookFeed,
                 taker_latency_ms: float = 410.0, speed_bump_ms: float = 250.0,
                 cancel_latency_ms: float = 150.0,
                 fak_min_fill_rate: float = 0.50, fak_min_attempts: int = 10,
                 fak_window_size: int = 30,
                 fill_realism: bool = True, capture: float = 0.30,
                 edge_contention: bool = True, race_loss_prob: float = 0.20,
                 feed_lag_ms: float = 150.0):
        self.portfolio = portfolio
        self.feed = feed
        self.taker_latency = taker_latency_ms / 1000.0
        # the uncancellable hold is a subset of the total tick-to-trade budget;
        # the rest is signal travel + Dublin decide/submit (the controllable bit)
        self.speed_bump = min(speed_bump_ms / 1000.0, self.taker_latency)
        self.cancel_latency = cancel_latency_ms / 1000.0
        # ---- snipe fill-realism haircut (paper only) ----
        # The naive paper model wins the full displayed size of every stale cheap
        # quote, uncontested — the dominant reason paper PnL overstates live. These
        # knobs model the snipe RACE you actually face from Dublin:
        #   capture        — avg fraction of the displayed top-of-book size we win
        #   edge_contention — richer (more underpriced) quotes are more contested,
        #                     so capture scales toward 0 as the discount to mid grows
        #   race_loss_prob — chance a faster/colocated arb takes the whole quote
        #                    before our order lands (a full miss)
        #   feed_lag       — our WS book trails the matching engine; we re-validate
        #                    against a book this much fresher, so favourable moves
        #                    are more likely to have richened the quote away
        # All off (capture=1, race_loss_prob=0, feed_lag=0) reproduces the old model.
        self.fill_realism = bool(fill_realism)
        self.capture = max(0.0, min(1.0, capture)) if self.fill_realism else 1.0
        self.edge_contention = bool(edge_contention) and self.fill_realism
        self.race_loss_prob = (max(0.0, min(1.0, race_loss_prob))
                               if self.fill_realism else 0.0)
        self.feed_lag = max(0.0, feed_lag_ms / 1000.0) if self.fill_realism else 0.0
        self.open_orders: dict[str, OpenOrder] = {}
        # taker orders frozen inside the speed bump: cannot be cancelled
        self._committed: set[str] = set()
        self.fak_stats = FakStats(fak_min_fill_rate, fak_min_attempts, fak_window_size)
        self._ids = itertools.count(1)
        feed.on_trade.append(self._on_trade)
        portfolio.summary_hooks.append(self.fak_stats.summary_lines)

    def _mid(self, token: str) -> float | None:
        """Book mid for a token — our observable proxy for the market's fair
        P(side). Its move during the hold is the BTC drift priced in."""
        book = self.feed.books.get(token)
        if not book:
            return None
        bid = book.best_bid()
        ask = book.best_ask()
        if bid and ask:
            return (bid[0] + ask[0]) / 2.0
        if ask:
            return ask[0]
        if bid:
            return bid[0]
        return None

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

    async def _speed_bump_fill(self, oid: str, market: Market, outcome: str,
                               token: str, price: float, shares: float,
                               leg: str) -> None:
        """Model the itode speed bump rather than a cancellable race.

        Timeline: the controllable portion (signal travel + Dublin decide/
        submit) elapses first, then the order enters a fixed, uncancellable
        hold (speed_bump). During the hold the order is committed — no cancel
        can pull it. At the end of the hold we re-validate against the book:
          * side richened (moved in our favour) -> cheap quote gone, rejected;
          * side cheapened (moved against us)   -> committed fill, adverse.
        """
        self.fak_stats.record_attempt()
        pre_bump = max(0.0, self.taker_latency - self.speed_bump)
        # only the controllable network/decision slice carries jitter; the
        # speed bump itself is a fixed hold imposed by the exchange
        await asyncio.sleep(pre_bump * random.uniform(0.5, 1.5))

        # fair (book mid) entering the uncancellable hold
        mid0 = self._mid(token)
        self._committed.add(oid)
        try:
            await asyncio.sleep(self.speed_bump)
        finally:
            self._committed.discard(oid)

        # Our WS book trails the matching engine; wait the feed lag so the
        # re-validation reflects the book the engine actually had when our order
        # landed (favourable moves are more likely to have richened the quote
        # away by then, and continuation moves show up as worse adverse drift).
        if self.feed_lag > 0:
            await asyncio.sleep(self.feed_lag)

        if market.close_ts <= time.time():
            self.fak_stats.record_kill()
            return  # window closed during the hold

        # re-validate against the (lag-adjusted) post-bump book
        book = self.feed.books.get(token)
        ask = book.best_ask() if book else None
        mid1 = self._mid(token)
        drift = (mid1 - mid0) if (mid0 is not None and mid1 is not None) else 0.0

        if not ask or ask[1] <= 0 or ask[0] > price + self.EPS:
            # the side richened (BTC moved our way) or the quote vanished: a
            # marketable FAK limit at our price can't reach it -> rejected.
            # This is the favourable-miss arm of adverse selection: we only
            # keep the prints that turned out bad.
            self.fak_stats.record_kill()
            log.info("paper FAK rejected: %s %s, ask %s vs limit %.3f after "
                     "%.0fms hold (fair drift %+.3f — quote richened/gone)",
                     market.title, outcome.upper(),
                     f"{ask[0]:.3f}" if ask else "gone", price,
                     self.speed_bump * 1000, drift)
            return

        # Competition: faster/colocated arbs race us for this same stale quote.
        # Sometimes they take it all before our order lands (full miss)...
        if self.race_loss_prob > 0 and random.random() < self.race_loss_prob:
            self.fak_stats.record_kill()
            log.info("paper FAK lost the race: %s %s, quote %.3f taken by faster "
                     "takers before our order landed", market.title, outcome.upper(),
                     ask[0])
            return

        # ...otherwise we win only a FRACTION of the displayed size, and the
        # richer (more underpriced vs mid) the quote, the more contested it is.
        capture = self.capture
        if self.edge_contention and mid1 is not None:
            discount = max(0.0, mid1 - ask[0])              # how far below fair
            contention = min(1.0, discount / self.CONTENTION_SCALE)
            capture *= (1.0 - contention)
        fill_sz = min(shares, ask[1] * capture)
        if fill_sz <= self.EPS:
            self.fak_stats.record_kill()
            log.info("paper FAK fully contested: %s %s @ %.3f — capture ~0 of %.0f "
                     "sh displayed (richly underpriced, so everyone races it)",
                     market.title, outcome.upper(), ask[0], ask[1])
            return

        # committed fill. We cannot escape the hold, so we take the print even
        # though the side may have cheapened against us while we were frozen.
        self.fak_stats.record_fill()
        if drift < -self.EPS:
            self.fak_stats.record_adverse(-drift)
            log.info("paper ADVERSE FILL: %s %s @ %.3f — fair drifted %+.3f "
                     "against us during the %.0fms uncancellable hold",
                     market.title, outcome.upper(), ask[0], drift,
                     self.speed_bump * 1000)
        if fill_sz < shares - self.EPS:
            log.info("paper PARTIAL FILL: %s %s %.0f/%.0f sh @ %.3f "
                     "(won %.0f%% of %.0f displayed — competition haircut)",
                     market.title, outcome.upper(), fill_sz, shares, ask[0],
                     100 * capture, ask[1])
        self.portfolio.on_fill(market, outcome, ask[0], fill_sz, taker=True, leg=leg)

    async def place_buy(self, market: Market, outcome: str, price: float, shares: float,
                        leg: str = "mm", extra: dict | None = None) -> str | None:
        # `extra` is observation-only metadata (e.g. distance-to-strike) used by
        # the live shadow logger; paper mode has no shadow logger, so ignore it.
        if shares < MIN_SHARES:
            return None
        token = market.token_up if outcome == "up" else market.token_down
        if leg != "mm":
            # taker legs hit the itode speed bump: once submitted the order is
            # committed for a fixed uncancellable hold and re-validated only at
            # the end (see _speed_bump_fill). No first-come race, no cancel.
            oid = f"paper-snipe-{next(self._ids)}"
            asyncio.ensure_future(
                self._speed_bump_fill(oid, market, outcome, token, price, shares, leg))
            return oid
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
        if order_id in self._committed:
            # frozen inside the itode speed bump: no cancel escape. The
            # jump-guard pull the strategy *thinks* it got does not exist live.
            log.info("paper cancel ignored: %s is in the 250ms uncancellable hold",
                     order_id)
            return
        # a resting GTC quote's cancel takes a round trip too: it stays hittable
        # while the cancel is in flight (this is when jump-guard pulls get
        # picked off)
        if self.cancel_latency > 0 and order_id in self.open_orders:
            asyncio.get_running_loop().call_later(
                self.cancel_latency, lambda: self.open_orders.pop(order_id, None))
        else:
            self.open_orders.pop(order_id, None)

    async def cancel_orders(self, order_ids) -> None:
        for oid in list(order_ids):
            await self.cancel(oid)

    async def cancel_all(self) -> None:
        for oid in list(self.open_orders):
            await self.cancel(oid)

    async def cancel_market(self, market: Market) -> None:
        # committed taker orders cannot be pulled; only resting GTC quotes go
        for oid in list(self.open_orders):
            if (oid not in self._committed
                    and self.open_orders[oid].market.slug == market.slug):
                del self.open_orders[oid]


class LiveExecutor:
    """Real orders through the Polymarket CLOB via py-clob-client-v2 (CLOB V2).

    Built for a signature_type 0 (plain EOA) wallet: the EOA signs orders and
    holds positions/collateral directly, so — unlike a deposit wallet — no
    per-order balance-allowance sync sits on the critical path. Combined with
    the speed bump being gone on crypto markets, the live order path is just
    sign-locally + one HTTP POST, i.e. as fast as the venue allows.
    """

    def __init__(self, portfolio: Portfolio, host: str, chain_id: int,
                 private_key: str, funder: str | None, signature_type: int,
                 onchain=None, fak_min_fill_rate: float = 0.50,
                 fak_min_attempts: int = 10, fak_window_size: int = 30,
                 presign: bool = False, presign_refresh_sec: float = 2.0,
                 presign_price_radius_ticks: int = 1,
                 presign_amount_buckets_usd: tuple | None = None,
                 presign_amount_tol: float = 0.7,
                 presign_max_age_sec: float = 45.0,
                 shadow=None):
        from py_clob_client_v2 import ClobClient

        self.portfolio = portfolio
        # optional ShadowTakerLogger: records seen-book/latency/fill-capture/
        # markouts on every live taker FAK (ROADMAP P0.2). Pure observation.
        self.shadow = shadow
        kwargs = {"key": private_key, "chain_id": chain_id,
                  "signature_type": signature_type}
        if funder:  # EOA (type 0) holds funds itself; only proxies need a funder
            kwargs["funder"] = funder
        self.client = ClobClient(host, **kwargs)
        self.client.set_api_creds(self.client.create_or_derive_api_key())
        self.signature_type = signature_type
        self.address = funder or self.client.get_address()
        self.open_orders: dict[str, OpenOrder] = {}
        self.fak_stats = FakStats(fak_min_fill_rate, fak_min_attempts, fak_window_size)
        self._seen_trades: set[str] = set()
        self.onchain = onchain
        # ---- EIP-712 pre-signing (snipe/scalp fast path) ----
        # The background presigner (run_presigner) always pre-warms the CLOB
        # market-info cache for active tokens so the first taker order per
        # market never pays a network round trip inside create_market_order.
        # When `presign` is on it additionally keeps a ladder of pre-signed
        # FAK orders around the live best ask, keyed by (price, $ stake); the
        # taker path posts a matching one directly (signing already done).
        self._presign_enabled = bool(presign)
        self._presign_refresh = max(0.25, float(presign_refresh_sec))
        self._presign_radius = max(0, int(presign_price_radius_ticks))
        self._presign_buckets = tuple(sorted(
            round(float(b), 2) for b in (presign_amount_buckets_usd or ())))
        self._presign_amount_tol = float(presign_amount_tol)
        self._presign_max_age = float(presign_max_age_sec)
        # token -> {(price, amount): (signed_order, signed_at)}
        self._presigned: dict[str, dict[tuple[float, float], tuple]] = {}
        self._warmed_tokens: set[str] = set()
        # monotonic ts of the last successful CLOB keep-warm ping (run_keepwarm).
        # py_clob_client_v2 posts orders over a module-level httpx client whose
        # keep-alive idles out after ~5s; our FAKs fire minutes apart, so this
        # tells the shadow log whether a slow POST rode a cold (reconnecting)
        # socket vs genuine server-side matching time.
        self._last_warm = 0.0
        self._merge_queue: dict[str, Market] = {}   # condition_id -> market
        self._redeem_queue: dict[str, Market] = {}
        self._tracked_conditions: set[str] = set()
        self._user_reconnect = asyncio.Event()
        if onchain is not None:
            portfolio.merge_hook = lambda market, pairs: self._merge_queue.setdefault(
                market.condition_id, market)
        portfolio.summary_hooks.append(self.fak_stats.summary_lines)
        native_sign, backend_name = _signing_backend()
        if native_sign:
            log.info("EIP-712 signing backend: %s (native, ~0.15ms/order)", backend_name)
        else:
            log.warning("EIP-712 signing backend: %s — PURE PYTHON, ~3.5ms/order. "
                        "Install `coincurve` for the ~22x faster native backend "
                        "(critical for the snipe race).", backend_name)
        log.info("live executor ready (CLOB v2, signature_type %d, address %s..., "
                 "pre-sign %s)", signature_type, self.address[:10],
                 "on" if self._presign_enabled else "off (prewarm only)")

    def collateral_balance(self, sync: bool = True) -> float:
        """Current pUSD collateral the CLOB credits this wallet, in USDC units.

        For proxy/deposit wallets (type 1/2/3) the CLOB caches the balance, so
        we push a sync first; an EOA (type 0) is read directly. Used to seed the
        portfolio's equity baseline from real funds instead of a config guess.
        """
        from py_clob_client_v2 import AssetType, BalanceAllowanceParams
        p = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL,
                                   signature_type=self.signature_type)
        if sync and self.signature_type in (1, 2, 3):
            try:
                self.client.update_balance_allowance(p)
            except Exception as e:  # noqa: BLE001
                log.warning("collateral balance sync failed: %s", e)
        ba = self.client.get_balance_allowance(p)
        try:
            return int(ba.get("balance", 0)) / 1e6
        except (TypeError, ValueError):
            return 0.0

    async def run_keepwarm(self, interval_sec: float = 3.0) -> None:
        """Keep the shared CLOB HTTP/2 connection warm so a taker FAK is just a
        warm POST, not a fresh TLS+connection setup.

        py_clob_client_v2 sends every order over a module-level httpx.Client
        whose keep-alive idles out after ~5s (httpx default). Our taker FAKs
        fire minutes apart and nothing else touches that client between them
        (market refresh uses a separate aiohttp session; reconcile only runs
        every 30s), so most snipes pay a cold reconnect on the critical path
        and inflate the submit->ack tail. A cheap GET /time on the shared
        client every few seconds keeps the socket open. Read-only; never
        trades, and a failed ping never stops the bot."""
        interval = max(1.0, float(interval_sec))
        while True:
            try:
                await asyncio.to_thread(self.client.get_server_time)
                self._last_warm = time.monotonic()
            except Exception as e:  # noqa: BLE001 — keep-warm must never break trading
                log.debug("keep-warm ping failed: %s", e)
            await asyncio.sleep(interval)

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

    @staticmethod
    def _stamp_taker_timing(attempt: dict | None, t_build0: float,
                            t_build1: float | None, presigned: bool) -> None:
        """Record, into the shadow attempt, the split of submit->ack latency:
        BUILD (order construction/sign, or a pre-signed lookup) vs POST (the
        post_order HTTP round trip). build_ms is None if create_market_order
        itself raised before completing. Pure observation — never raises."""
        try:
            now = time.monotonic()
            build_ms = round((t_build1 - t_build0) * 1000, 1) if t_build1 else None
            post_ms = round((now - t_build1) * 1000, 1) if t_build1 else None
            log.info("TAKER TIMING build %s post %s presigned=%s",
                     f"{build_ms:.0f}ms" if build_ms is not None else "n/a",
                     f"{post_ms:.0f}ms" if post_ms is not None else "n/a", presigned)
            if attempt is not None:
                attempt["build_ms"] = build_ms
                attempt["post_ms"] = post_ms
                attempt["presigned"] = bool(presigned)
        except Exception as e:  # noqa: BLE001 — never let instrumentation break a trade
            log.debug("taker timing stamp failed: %s", e)

    async def place_buy(self, market: Market, outcome: str, price: float, shares: float,
                        leg: str = "mm", extra: dict | None = None) -> str | None:
        from py_clob_client_v2 import (
            MarketOrderArgs, OrderArgs, OrderType, PartialCreateOrderOptions, Side,
        )

        if shares < MIN_SHARES:
            return None
        token = market.token_up if outcome == "up" else market.token_down
        opts = PartialCreateOrderOptions(neg_risk=market.neg_risk)
        price = round(price, 3)
        shares = round(shares, 2)

        if leg == "mm":
            # resting maker quote (GTC). Fills arrive asynchronously on the
            # authenticated user feed (handled in _handle_user_msg).
            try:
                signed = await asyncio.to_thread(
                    self.client.create_order,
                    OrderArgs(price=price, size=shares, side=Side.BUY,
                              token_id=token, expiration=0),
                    opts)
                resp = await asyncio.to_thread(self.client.post_order, signed, OrderType.GTC)
            except Exception as e:
                log.warning("MM order rejected: %s", e)
                return None
            oid = (resp.get("orderID") or resp.get("orderId")) if isinstance(resp, dict) else None
            if oid:
                self.open_orders[oid] = OpenOrder(oid, market, outcome, token, price, shares, leg=leg)
            else:
                log.warning("MM order returned no id: %s", resp)
            return oid

        # taker leg (snipe/scalp): marketable fill-and-kill. A market BUY is
        # quoted in COLLATERAL (pUSD), rounded to 2dp, capped at our limit
        # price — never leaves a remainder resting. The fill is settled
        # synchronously from the response (not via the user feed), so the
        # position is recorded the moment the FAK returns.
        amount = round(shares * price, 2)
        if amount <= 0:
            return None
        self.fak_stats.record_attempt()
        # shadow logger: snapshot the book we're acting on and start the latency
        # clock right before submission (ROADMAP P0.2). No-op if not wired.
        shadow_attempt = (self.shadow.on_submit(market, outcome, token, price, shares, leg,
                                                extra=extra)
                          if self.shadow is not None else None)
        # fast path: a pre-signed order whose stake bucket is <= the desired
        # stake (never oversizes) and within tolerance keeps EIP-712 signing
        # off the critical path — fire is then just the HTTP POST below.
        used_amount = amount
        presigned = self._take_presigned(token, price, amount) if self._presign_enabled else None
        # Split the submit->ack latency into BUILD (create_market_order, the
        # client-side market-info/sizing/sign step — or a pre-signed lookup) vs
        # POST (the post_order HTTP round trip). The shadow log showed ~1.5s of
        # submit latency against a ~1ms network RTT, so we need to know which
        # of the two owns it. presigned=true rows isolate the POST-only cost.
        t_build0 = time.monotonic()
        if shadow_attempt is not None:
            # how long since the CLOB connection was last kept warm (run_keepwarm).
            # Lets analyze_shadow separate a slow POST caused by a cold reconnect
            # from genuine server-side matching latency. None = keep-warm not running.
            shadow_attempt["warm_age_ms"] = (round((t_build0 - self._last_warm) * 1000, 1)
                                             if self._last_warm else None)
        t_build1 = None
        try:
            if presigned is not None:
                signed, used_amount = presigned
            else:
                signed = await asyncio.to_thread(
                    self.client.create_market_order,
                    MarketOrderArgs(token_id=token, amount=used_amount, side=Side.BUY,
                                    price=price, order_type=OrderType.FAK),
                    opts)
            t_build1 = time.monotonic()
            resp = await asyncio.to_thread(self.client.post_order, signed, OrderType.FAK)
        except Exception as e:
            self._stamp_taker_timing(shadow_attempt, t_build0, t_build1, presigned is not None)
            log.warning("taker order rejected: %s", e)
            self.fak_stats.record_kill()
            if self.shadow is not None:
                self.shadow.on_result(shadow_attempt, 0.0, 0.0, f"rejected: {e}")
            return None
        self._stamp_taker_timing(shadow_attempt, t_build0, t_build1, presigned is not None)
        requested = used_amount / price if price > 0 else shares
        filled = _parse_fill_shares(resp, requested)
        avg = _parse_fill_price(resp, price) if filled > 0 else 0.0
        if self.shadow is not None:
            status = str(resp.get("status")) if isinstance(resp, dict) else "no-fill"
            self.shadow.on_result(shadow_attempt, filled, avg, status)
        if filled > 0:
            self.fak_stats.record_fill()
            self._log_fee_ground_truth(market, outcome, avg, filled, resp)
            self.portfolio.on_fill(market, outcome, avg, filled, taker=True, leg=leg)
        else:
            self.fak_stats.record_kill()
            log.info("live FAK killed: %s %s @ %.3f (no fill)",
                     market.title, outcome.upper(), price)
        return (resp.get("orderID") or resp.get("orderId")) if isinstance(resp, dict) else None

    def _log_fee_ground_truth(self, market: Market, outcome: str, avg: float,
                              shares: float, resp) -> None:
        """Record the REAL taker fee from a live fill next to what our model
        assumed, so we can confirm/calibrate the paper bot's fee formula
        (rate * p*(1-p) * shares). Polymarket's published schedule (Gamma) and
        the CLOB's base-fee field don't obviously agree, and the p*(1-p) vs
        min(p,1-p) form is unverified — a real fill is the only ground truth.

        Dumps whatever fee/amount fields the response actually carries; on a
        BUY the fee is taken in shares, so collateral_paid/shares - avg ~ the
        per-share fee when the response reports both legs."""
        modeled = market.taker_fee_per_share(avg) * shares
        raw = {k: resp.get(k) for k in (
            "fee", "feeRateBps", "fee_rate_bps", "makerFeeRateBps",
            "makingAmount", "making_amount", "takingAmount", "taking_amount",
            "price", "status") if isinstance(resp, dict) and k in resp} or "(none in response)"
        implied = None
        if isinstance(resp, dict):
            making = resp.get("makingAmount") or resp.get("making_amount")
            taking = resp.get("takingAmount") or resp.get("taking_amount")
            try:
                if making and taking and float(taking) > 0:
                    implied = float(making) / float(taking) - avg  # per-share fee, if any
            except (TypeError, ValueError):
                implied = None
        log.info("FEE CHECK %s %s: modeled $%.4f (rate %.3f exp %.1f, %.2f sh @ %.3f, "
                 "%.4f/sh)%s | live fill fields: %s", market.title, outcome.upper(),
                 modeled, market.fee_rate, market.fee_exponent, shares, avg,
                 modeled / shares if shares else 0.0,
                 f" | implied {implied:+.4f}/sh" if implied is not None else "", raw)

    # ---------- EIP-712 pre-signing ----------

    def _build_taker_order(self, token: str, amount: float, price: float, neg_risk: bool):
        """Build + EIP-712 sign one marketable FAK BUY (collateral = `amount`).
        Pure-CPU once the token's market info is cached (see run_presigner)."""
        from py_clob_client_v2 import (
            MarketOrderArgs, OrderType, PartialCreateOrderOptions, Side,
        )
        return self.client.create_market_order(
            MarketOrderArgs(token_id=token, amount=round(amount, 2), side=Side.BUY,
                            price=round(price, 3), order_type=OrderType.FAK),
            PartialCreateOrderOptions(neg_risk=neg_risk))

    def _take_presigned(self, token: str, price: float, amount: float):
        """Pop the best pre-signed order for (token, price): the largest stake
        bucket that does not exceed `amount` and is within `amount_tol` of it.
        Returns (signed_order, bucket_amount) or None (-> live sign)."""
        slots = self._presigned.get(token)
        if not slots:
            return None
        now = time.time()
        floor = amount * self._presign_amount_tol
        best_key = None
        best_amt = -1.0
        for key, (_signed, ts) in list(slots.items()):
            p, amt = key
            if now - ts > self._presign_max_age:
                del slots[key]
                continue
            if abs(p - price) > 1e-9 or amt > amount + 1e-9 or amt < floor:
                continue
            if amt > best_amt:
                best_amt, best_key = amt, key
        if best_key is None:
            return None
        signed, _ts = slots.pop(best_key)
        return signed, best_key[1]

    async def _refresh_token_ladder(self, market: Market, token: str, best_ask: float) -> None:
        tick = market.tick or 0.01
        center = round(best_ask / tick) * tick
        prices = []
        for k in range(-self._presign_radius, self._presign_radius + 1):
            p = round(center + k * tick, 3)
            if tick <= p <= 1 - tick:
                prices.append(p)
        slots = self._presigned.setdefault(token, {})
        now = time.time()
        for p in prices:
            for amt in self._presign_buckets:
                cur = slots.get((p, amt))
                if cur and now - cur[1] <= self._presign_max_age:
                    continue  # still fresh
                try:
                    signed = await asyncio.to_thread(
                        self._build_taker_order, token, amt, p, market.neg_risk)
                    slots[(p, amt)] = (signed, time.time())
                except Exception as e:  # noqa: BLE001 — one bad level shouldn't stall the ladder
                    log.debug("presign build %s @ %.3f $%.0f failed: %s",
                              market.title, p, amt, e)
        keep = set(prices)
        for key in list(slots):  # drop levels that drifted out of the window
            if key[0] not in keep:
                del slots[key]

    async def run_presigner(self, feed, markets_provider) -> None:
        """Background loop. Always pre-warms the CLOB market-info cache for
        active tokens (so the first taker order per market is sign-only, no
        network). When pre-signing is enabled it also keeps a small ladder of
        pre-signed FAK orders around each token's live best ask."""
        while True:
            await asyncio.sleep(self._presign_refresh)
            try:
                markets = list(markets_provider())
            except Exception as e:  # noqa: BLE001
                log.debug("presigner: market provider failed: %s", e)
                continue
            live_tokens: set[str] = set()
            for m in markets:
                for token in (m.token_up, m.token_down):
                    live_tokens.add(token)
                    if token not in self._warmed_tokens:
                        try:
                            await asyncio.to_thread(
                                self.client.get_clob_market_info, m.condition_id)
                            self._warmed_tokens.add(token)
                        except Exception as e:  # noqa: BLE001
                            log.debug("presign prewarm %s failed: %s", m.title, e)
                            continue
                    if not self._presign_enabled or not self._presign_buckets:
                        continue
                    book = feed.books.get(token)
                    ask = book.best_ask() if book else None
                    if ask:
                        await self._refresh_token_ladder(m, token, ask[0])
            for token in list(self._presigned):  # forget markets we no longer trade
                if token not in live_tokens:
                    del self._presigned[token]

    async def cancel(self, order_id: str) -> None:
        from py_clob_client_v2 import OrderPayload
        try:
            await asyncio.to_thread(self.client.cancel_order, OrderPayload(orderID=order_id))
        except Exception as e:
            log.warning("cancel failed %s: %s", order_id, e)
        self.open_orders.pop(order_id, None)

    async def cancel_orders(self, order_ids) -> None:
        """Cancel many resting orders in a SINGLE round trip — for yanking a
        whole book of quotes the instant BTC swings or a window closes."""
        ids = [oid for oid in dict.fromkeys(order_ids) if oid]
        if not ids:
            return
        try:
            await asyncio.to_thread(self.client.cancel_orders, ids)
        except Exception as e:
            log.warning("batch cancel of %d orders failed: %s", len(ids), e)
        for oid in ids:
            self.open_orders.pop(oid, None)

    async def cancel_all(self) -> None:
        """Cancel every resting order for this account in one call (kill switch
        / feed stall). Pre-signed FAK orders are unaffected (unique salt, no
        nonce bump), but they aren't resting so nothing here touches them."""
        try:
            await asyncio.to_thread(self.client.cancel_all)
        except Exception as e:
            log.warning("cancel-all failed: %s", e)
        self.open_orders.clear()

    async def cancel_market(self, market: Market) -> None:
        """Cancel all of our resting orders in one market, server-side in a
        single call; fall back to a batch by id if the market cancel errors."""
        from py_clob_client_v2 import OrderMarketCancelParams
        try:
            await asyncio.to_thread(self.client.cancel_market_orders,
                                    OrderMarketCancelParams(market=market.condition_id))
        except Exception as e:
            log.warning("market cancel failed for %s: %s; falling back to batch",
                        market.title, e)
            await self.cancel_orders(
                oid for oid, o in self.open_orders.items() if o.market.slug == market.slug)
            return
        for oid, o in list(self.open_orders.items()):
            if o.market.slug == market.slug:
                self.open_orders.pop(oid, None)

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
            # Resting maker (GTC) fills — the bulk of this strategy. Taker
            # (FAK) fills are settled synchronously in place_buy, so they are
            # never tracked here (not added to open_orders) and can't double-count.
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
        connected_once = False
        fails = 0
        while True:
            if not self._tracked_conditions:
                await asyncio.sleep(1)
                continue
            self._user_reconnect.clear()
            markets = sorted(self._tracked_conditions)
            try:
                async with websockets.connect(USER_WS_URL, ping_interval=None) as ws:
                    await ws.send(json.dumps({"type": "user", "markets": markets, "auth": auth}))
                    if fails >= WS_WARN_AFTER:
                        log.info("user feed reconnected, subscribed to %d markets", len(markets))
                    elif not connected_once:
                        log.info("user feed subscribed to %d markets", len(markets))
                    else:
                        log.debug("user feed resubscribed to %d markets", len(markets))
                    connected_once = True
                    fails = 0

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
                fails += 1
                if fails >= WS_WARN_AFTER:
                    log.warning("user ws error: %s; reconnecting in 2s (%d consecutive)", e, fails)
                else:
                    log.debug("user ws error: %s; reconnecting in 2s", e)
                await asyncio.sleep(2)
