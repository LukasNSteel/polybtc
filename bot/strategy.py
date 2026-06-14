"""The three strategy legs: fair-value market making, stale-quote sniping,
and end-of-window near-certainty scalping."""

import asyncio
import logging
import math
import os
import time

from .binance_feed import BinanceFeed
from .execution import MIN_SHARES, Portfolio
from .fair_value import blend_with_market, prob_up, prob_up_bounds
from .guards import FillGuards, JumpGuard, MarkoutTracker
from .markets import Market, MarketManager
from .orderbook import OrderBookFeed

log = logging.getLogger("strategy")


def round_tick(price: float, tick: float) -> float:
    return round(round(price / tick) * tick, 4)


class Strategy:
    def __init__(self, cfg, binance: BinanceFeed, markets: MarketManager,
                 feed: OrderBookFeed, executor, portfolio: Portfolio,
                 paper: bool = False):
        self.cfg = cfg
        self.paper = paper
        self.binance = binance
        self.markets = markets
        self.feed = feed
        self.exec = executor
        self.portfolio = portfolio
        # slug -> {"up": (order_id, price), "dn": (order_id, price)}
        self.quotes: dict[str, dict[str, tuple[str, float]]] = {}
        self._cooldown: dict[tuple[str, str, str], float] = {}  # (slug, leg, side) -> ts
        self.killed = False
        self._kill_count = 0
        # kill switch baseline: equity at session start, not all-time start
        # cash — otherwise banked profit from earlier sessions lets a bad
        # session bleed far past the configured limit before tripping
        # (session 1781182217 lost $660 against a $200 kill switch this way)
        self._session_start_equity: float | None = None
        self._last_status = 0.0
        self._last_calib = 0.0
        log_dir = cfg.get("log_dir", default="logs")
        os.makedirs(log_dir, exist_ok=True)
        self._calib_path = os.path.join(log_dir, "calibration.csv")
        if not os.path.exists(self._calib_path):
            with open(self._calib_path, "w") as f:
                f.write("ts,kind,slug,t_remaining,p_up,model_p,outcome\n")
        self._model_marks: dict[str, float] = {}

        g = cfg.get("guards", default={}) or {}
        self.jump_guard = JumpGuard(
            window_sec=g.get("jump_window_sec", 3.0),
            sigma=g.get("jump_sigma", 5.0),
            min_move=g.get("jump_min_move_bps", 5.0) * 1e-4,
            cooldown_sec=g.get("jump_cooldown_sec", 10.0),
        )
        self.fill_guards = FillGuards(
            fade_per_fill=g.get("fade_per_fill", 0.005),
            fade_window_sec=g.get("fade_window_sec", 60),
            fade_max=g.get("fade_max", 0.02),
            breaker_fills=g.get("breaker_fills", 4),
            breaker_window_sec=g.get("breaker_window_sec", 60),
            breaker_cooldown_sec=g.get("breaker_cooldown_sec", 180),
        )
        self.markouts = MarkoutTracker(
            tuple(float(h) for h in g.get("markout_horizons_sec", [10, 60])))
        portfolio.fill_hook = self._on_fill
        portfolio.summary_hooks.append(self.markouts.summary_lines)

    def on_liquidation(self, side: str, qty: float, px: float,
                       notional: float, now: float) -> None:
        """Large perp liquidation — pull MM quotes before the cascade hits spot."""
        pause = self.cfg.get("liquidations", "pause_sec", default=8)
        self.jump_guard.force_pause(
            now, pause, f"liquidation {side} ${notional / 1000:.0f}k @ {px:.0f}")

    def _calib_write(self, line: str) -> None:
        with open(self._calib_path, "a") as f:
            f.write(line + "\n")

    def _on_fill(self, market: Market, outcome: str, price: float, shares: float,
                 taker: bool, leg: str) -> None:
        now = time.time()
        token = market.token_up if outcome == "up" else market.token_down
        self.markouts.record_fill(token, price, leg, now)
        if leg == "mm" and not taker:
            self.fill_guards.record_fill(market.slug, outcome, now, market.title)

    def _token_mid(self, token: str) -> float | None:
        book = self.feed.books.get(token)
        if not book:
            return None
        bb, ba = book.best_bid(), book.best_ask()
        if not bb or not ba:
            return None
        return (bb[0] + ba[0]) / 2

    # ---------- helpers ----------

    def _cooled(self, slug: str, leg: str, side: str, sec: float) -> bool:
        key = (slug, leg, side)
        now = time.time()
        if now - self._cooldown.get(key, 0) < sec:
            return False
        self._cooldown[key] = now
        return True

    def _exposure_ok(self, extra_usd: float) -> bool:
        cap = self.cfg.get("risk", "max_total_exposure_usd", default=500)
        return self.portfolio.exposure() + extra_usd <= cap

    def _position_cost(self, slug: str) -> float:
        p = self.portfolio.positions.get(slug)
        return p.cost if p else 0.0

    def _cutoff_sec(self, m: Market) -> float:
        key = {"5m": "cutoff_sec_5m", "15m": "cutoff_sec_15m",
               "4h": "cutoff_sec_4h"}.get(m.kind, "cutoff_sec_hourly")
        return self.cfg.get("trading", key, default=0)

    def _tradable(self, m: Market) -> bool:
        """Polymarket halts some markets before expiry; also apply our own cutoff."""
        return m.accepting and m.t_remaining > self._cutoff_sec(m)

    def _book_fresh(self, token: str, max_age: float = 15.0) -> bool:
        book = self.feed.books.get(token)
        return book is not None and time.time() - book.ts < max_age

    def _vol_warm(self) -> bool:
        """No taker trades until the vol EWMA has real data behind it. At
        startup vol_per_sec sits at the min_vol floor, which inflates the
        model's d (and the dual-beta robust bounds) ~3x: session 1781248550
        sniped both sides of one window in its first 14 seconds and lost $67
        on fake 11-14c edges that vanish under warmed-up vol."""
        warmup = self.cfg.get("risk", "vol_warmup_sec", default=90)
        warm = self.binance.vol_age >= warmup
        if not warm and self._cooled("_global", "vol-warmup", "all", 30):
            log.info("vol warming up (%.0fs / %.0fs) — taker legs paused",
                     self.binance.vol_age, warmup)
        return warm

    def _market_mid(self, m: Market) -> float | None:
        """Implied P(up) from the order book, using both tokens' books."""
        estimates = []
        book = self.feed.books.get(m.token_up)
        if book:
            bb, ba = book.best_bid(), book.best_ask()
            if bb and ba and ba[0] - bb[0] < 0.25:
                estimates.append((bb[0] + ba[0]) / 2)
        book = self.feed.books.get(m.token_down)
        if book:
            bb, ba = book.best_bid(), book.best_ask()
            if bb and ba and ba[0] - bb[0] < 0.25:
                estimates.append(1 - (bb[0] + ba[0]) / 2)
        if not estimates:
            return None
        return sum(estimates) / len(estimates)

    def _momentum_z(self, window_sec: float) -> float | None:
        """Recent log-return in units of the vol expected over that window.
        Positive = price has been running up, negative = running down."""
        r = self.binance.recent_return(window_sec)
        if r is None:
            return None
        denom = self.binance.vol_per_sec * math.sqrt(window_sec)
        if denom <= 0:
            return None
        return r / denom

    def _inventory_imbalance(self, m: Market, p_up: float, cap: float | None = None) -> float:
        """Signed fraction of per-market cap tied up in directional inventory.
        Positive = long Up risk, negative = long Down risk."""
        p = self.portfolio.positions.get(m.slug)
        if p is None:
            return 0.0
        if cap is None:
            cap = self.cfg.get("market_maker", "max_position_usd", default=150)
        net = p.up * p_up - p.dn * (1 - p_up)
        return max(-1.0, min(1.0, net / cap))

    # ---------- legs ----------

    async def _market_make(self, m: Market, p_up: float) -> None:
        c = self.cfg["market_maker"]
        if not c["enabled"]:
            return
        state = self.quotes.setdefault(m.slug, {})
        stop_at = max(c["stop_quoting_sec"], self._cutoff_sec(m))
        now = time.time()
        if (not self._tradable(m) or m.t_remaining < stop_at
                or not self.jump_guard.allowed(now)
                or self._position_cost(m.slug) >= c["max_position_usd"]):
            for side in list(state):
                await self.exec.cancel(state.pop(side)[0])
            return
        # skew quotes away from the side we're already loaded on: the more
        # directional inventory we hold, the wider we quote that same risk
        imbalance = self._inventory_imbalance(m, p_up)
        skew_k = c.get("inventory_skew", 1.0)
        # widen the edge with remaining horizon: expected fair-value drift grows
        # ~sqrt(t_remaining), so an edge sized for a 5-minute window is far too
        # tight on 15m/hourly markets (where MM adverse selection concentrates)
        ref = c.get("edge_ref_sec", 300.0)
        horizon_mult = min(c.get("edge_max_mult", 3.0),
                           max(1.0, (m.t_remaining / ref) ** 0.5))
        # trend filter: in a directional run, the bid on the side the trend is
        # fading is the one that gets hit — repricing lags the move, so the
        # fade-side quote is structurally stale. Pull it instead of repricing.
        trend_sigma = c.get("trend_filter_sigma", 0.0)
        trend_z = (self._momentum_z(c.get("trend_window_sec", 45.0)) or 0.0
                   if trend_sigma > 0 else 0.0)
        for side, fair in (("up", p_up), ("dn", 1 - p_up)):
            if self.fill_guards.blocked(m.slug, side, now):
                if side in state:
                    await self.exec.cancel(state.pop(side)[0])
                continue
            trending_against = ((side == "up" and trend_z <= -trend_sigma)
                                or (side == "dn" and trend_z >= trend_sigma))
            if trending_against:
                if self._cooled(m.slug, "trend-pull", side, 30):
                    log.info("TREND PULL %s %s: momentum %.1f sigma against resting bid",
                             m.title, side.upper(), trend_z)
                if side in state:
                    await self.exec.cancel(state.pop(side)[0])
                continue
            same_risk = max(0.0, imbalance) if side == "up" else max(0.0, -imbalance)
            edge = (c["edge"] * horizon_mult * (1 + skew_k * same_risk)
                    + self.fill_guards.fade(m.slug, side, now))
            want = round_tick(fair - edge, m.tick)
            # post-only: never cross the spread (taking is the sniper's job)
            token = m.token_up if side == "up" else m.token_down
            book = self.feed.books.get(token)
            ask = book.best_ask() if book else None
            if ask:
                want = min(want, round_tick(ask[0] - m.tick, m.tick))
            # price band: never bid the deep tail of a nearly-decided market —
            # cheap "bargains" are where the model's fat tail overprices most
            # (the sniper's min_ask guard exists for the same reason), and the
            # post-only clamp would otherwise park penny bids that hoover up
            # thousands of worthless shares. High quotes are the mirror case.
            if not (c.get("min_quote", 0.10) <= want <= c.get("max_quote", 0.85)):
                if side in state:
                    await self.exec.cancel(state.pop(side)[0])
                continue
            cur = state.get(side)
            if cur and cur[0] not in getattr(self.exec, "open_orders", {}):
                cur = None  # order was filled; re-quote
                state.pop(side, None)
            if cur and abs(cur[1] - want) < c["reprice_threshold"]:
                continue
            if cur:
                await self.exec.cancel(cur[0])
                state.pop(side, None)
            # share cap: USD-only sizing balloons to 1000-share lots on cheap
            # quotes; keep it bounded even if min_quote is ever lowered
            shares = max(min(c["quote_size_usd"] / want,
                             c.get("max_quote_shares", 150)), MIN_SHARES)
            if want * shares > 0 and self._exposure_ok(want * shares):
                oid = await self.exec.place_buy(m, side, want, shares, leg="mm")
                if oid:
                    state[side] = (oid, want)

    async def _snipe(self, m: Market, p_up: float, p_lo: float, p_hi: float) -> None:
        """Taker leg. Gated on the DUAL-BETA ROBUST edge: the entry must clear
        min_edge under both the mean-reversion (beta~0.83) and momentum
        (beta~1.36) calibrations of the distance model, so it only fires on
        genuine book-lags-Binance mispricings, never on a regime bet.
        (p_lo/p_hi are the min/max P(up) across regimes; for a Down buy the
        conservative bound is 1 - p_hi.)

        FAVORITE-ONLY (June 12 live forensics, 307 settled paper fills):
        buys are restricted to asks in [min_ask, max_ask] ~ [0.50, 0.80] —
        the side the market itself already prices as the favorite. Underdog
        buys (ask < 0.5, i.e. betting the model's sign-flip against the
        book) won 18% and lost -$1,660 on $5.5k staked, negative on every
        market kind; favorite buys won 73% and made +$304, positive on
        every kind and both sides. The calibration log shows why: where the
        model disagrees with the market on the *sign*, the market wins
        (predicted 0.35 -> realized 0.45). We only trade lag in magnitude,
        never sign."""
        c = self.cfg["sniper"]
        if not c["enabled"] or not self._tradable(m) or not self._vol_warm():
            return
        per_mkt_cap = c.get("max_position_usd",
                            self.cfg.get("market_maker", "max_position_usd", default=150))
        imbalance = self._inventory_imbalance(m, p_up, cap=per_mkt_cap)
        max_inv = c.get("max_inventory_frac", 0.25)
        pos = self.portfolio.positions.get(m.slug)
        for side, fair, robust, token in (("up", p_up, p_lo, m.token_up),
                                          ("dn", 1 - p_up, 1 - p_hi, m.token_down)):
            # don't stack one side past the inventory fraction: same lagging
            # model, not independent edge (every cap-sized loser in session
            # 1781212299 was stacked one-sided inventory)
            same_risk = imbalance if side == "up" else -imbalance
            if same_risk >= max_inv:
                continue
            # never snipe AGAINST shares we already hold in this window: the
            # combined cost of an Up+Down pair bought near the mid exceeds $1
            # (guaranteed loss on the overlap), and a model that has flipped
            # sides mid-window is reporting noise, not two independent edges
            # (session 1781248550: 8 UP @ 0.52 then 145 DN @ 0.49 five seconds
            # later as spot wobbled across the open; settled -$67)
            opposite = (pos.dn if side == "up" else pos.up) if pos else 0.0
            if opposite > 1.0:
                continue
            if not self._book_fresh(token):
                continue  # never take against a possibly-stale view of the book
            book = self.feed.books.get(token)
            ask = book.best_ask() if book else None
            if not ask:
                continue
            ask_px, ask_sz = ask
            if ask_px < c.get("min_ask", 0.50):
                # underdog/sign-flip buys: 18% win rate, -$1,660 across all
                # sessions, negative on every market kind. The market is
                # right about the sign; we only trade lag in magnitude.
                continue
            if ask_px > c.get("max_ask", 0.80):
                # near-certainty asks are the scalper's job (with its much
                # stricter 0.997 probability gate); at 0.8+ the snipe edge
                # was fee-dead in live data (-21.6%)
                continue
            # edge must clear the taker fee under the WORST-case regime
            fee = m.taker_fee_per_share(ask_px)
            net_edge = robust - ask_px - fee
            max_edge = c.get("max_edge", 0.25)
            if net_edge > max_edge:
                # winner's-curse veto. The backtest said edge>0.25 prints were
                # the best bucket — but those prints are won by faster bots
                # (219 of our FAK orders died "lost the race"). What's still
                # on the book 350ms later at a giant apparent edge is what
                # they passed on: our edge>=0.15 fills went 0-for-5 (-104%).
                if self._cooled(m.slug, "snipe-veto", side, 30):
                    log.info("SNIPE VETO %s %s: edge %.3f exceeds max_edge %.2f",
                             m.title, side.upper(), net_edge, max_edge)
                continue
            if net_edge > c["min_edge"]:
                # size proportional to conviction: full size at 2x min_edge
                conviction = min(1.0, net_edge / (2 * c["min_edge"]))
                usd = min(c["max_take_usd"] * max(0.25, conviction), ask_px * ask_sz)
                shares = max(usd / ask_px, MIN_SHARES)
                if (self._exposure_ok(usd)
                        and self._position_cost(m.slug) + usd <= per_mkt_cap
                        and self._cooled(m.slug, "snipe", side, 10)):
                    log.info("SNIPE %s %s: ask %.3f + fee %.4f vs robust %.3f "
                             "(blend %.3f, edge %.3f, $%.0f)",
                             m.title, side.upper(), ask_px, fee, robust, fair,
                             net_edge, usd)
                    await self.exec.place_buy(m, side, ask_px, shares, leg="snipe")

    async def _scalp(self, m: Market, p_up: float) -> None:
        c = self.cfg["scalper"]
        # only the kinds where late high-price buys tested +EV: 1h +2.2%/$1
        # (14/15 days green), 15m mildly positive; 5m tested NEGATIVE
        # (-1.7%/$1, -4.6% at 0.97-0.99 30-60s out) — see research/REPORT.md
        kinds = c.get("kinds")
        if kinds and m.kind not in kinds:
            return
        # skip the final seconds (resolution-source noise: Chainlink/Binance
        # close prints can disagree with the book's last look); per-kind
        # override because the study's best 15m scalp bucket is tau 5-30s
        min_tau = c.get(f"min_tau_sec_{m.kind}", c.get("min_tau_sec", 0))
        if (not c["enabled"] or not self._tradable(m) or not self._vol_warm()
                or m.t_remaining > c["window_sec"] or m.t_remaining <= min_tau):
            return
        for side, fair, token in (("up", p_up, m.token_up), ("dn", 1 - p_up, m.token_down)):
            if fair < c["min_prob"]:
                continue
            book = self.feed.books.get(token)
            ask = book.best_ask() if book else None
            if not ask or ask[0] > c["max_price"]:
                continue
            if ask[0] < c.get("min_price", 0.90):
                # the scalp thesis is "pay up for a near-certainty in the
                # final seconds". A *cheap* ask on a side the model calls
                # certain means the whole market disagrees with us — that is
                # a model dispute, not a scalp, and the market wins those
                # (session 1781182217: 5557 sh @ 0.01, "fair" 0.9976, -$59)
                continue
            if fair - ask[0] - m.taker_fee_per_share(ask[0]) <= 0:
                continue  # fee eats the scalp
            if not self._book_fresh(token):
                continue
            usd = min(c["max_usd"], ask[0] * ask[1])
            shares = max(usd / ask[0], MIN_SHARES)
            if self._exposure_ok(usd) and self._cooled(m.slug, "scalp", side, 15):
                log.info("SCALP %s %s @ %.3f (fair %.4f, %.0fs left)",
                         m.title, side.upper(), ask[0], fair, m.t_remaining)
                await self.exec.place_buy(m, side, ask[0], shares, leg="scalp")

    # ---------- settlement (paper) ----------

    async def _settle_expired(self) -> None:
        still_waiting = []
        for m in self.markets.expired:
            if m.open_price is None:  # e.g. position restored after a restart
                m.open_price = await self.binance.kline_open(m.interval, m.open_ts)
            close = await self.binance.kline_close(m.interval, m.open_ts)
            if close is None or m.open_price is None:
                # keep retrying long enough for restored positions to resolve
                if time.time() - m.close_ts < 24 * 3600:
                    still_waiting.append(m)
                continue
            up_won = close >= m.open_price
            self.portfolio.settle(m, up_won)
            self._calib_write(f"{int(time.time())},{m.kind},{m.slug},0,,,{1 if up_won else 0}")
            if hasattr(self.exec, "queue_redeem"):
                self.exec.queue_redeem(m)
            self.quotes.pop(m.slug, None)
            self.fill_guards.drop_market(m.slug)
        self.markets.expired = still_waiting

    # ---------- main loop ----------

    async def run(self, tick_sec: float = 0.25) -> None:
        while True:
            await asyncio.sleep(tick_sec)
            try:
                await self._step()
            except Exception:
                log.exception("strategy step failed")

    async def _pull_all_quotes(self) -> None:
        for slug, state in self.quotes.items():
            for side in list(state):
                await self.exec.cancel(state.pop(side)[0])

    async def _step(self) -> None:
        if self.killed:
            return
        spot = self.binance.price
        if spot is None:
            return

        # never quote blind: if the price feed stalls, pull everything
        max_age = self.cfg.get("risk", "max_feed_age_sec", default=5)
        if self.binance.feed_age > max_age:
            log.warning("binance feed stale (%.1fs) — pulling all quotes", self.binance.feed_age)
            await self._pull_all_quotes()
            return

        now = time.time()
        # jump guard: a fast move means our resting MM bids are the stale
        # quotes — _market_make pulls them while the guard is tripped
        # (the sniper keeps running: a jump is exactly its moment)
        self.jump_guard.update(spot, self.binance.vol_per_sec, now)
        self.markouts.resolve(self._token_mid, now)

        # keep clob subscriptions in sync with active markets
        assets: set[str] = set()
        for m in self.markets.active.values():
            assets.add(m.token_up)
            assets.add(m.token_down)
        self.feed.set_assets(assets)
        if hasattr(self.exec, "track_markets"):  # live: user feed subscriptions
            self.exec.track_markets({m.condition_id for m in self.markets.active.values()})

        fv = self.cfg.get("fair_value", default={}) or {}
        # momentum drift: expected continuation of the recent run, scaled by
        # beta (<1 = partial continuation) and the fraction of the momentum
        # window still remaining. Clamped in sigma units so a freak print can
        # never dominate the diffusion term.
        mom_beta = fv.get("momentum_beta", 0.0)
        mom_window = fv.get("momentum_window_sec", 60.0)
        mom_clamp = fv.get("momentum_clamp_sigma", 1.5)
        r_recent = self.binance.recent_return(mom_window) if mom_beta > 0 else None
        marks: dict[str, float] = {}
        for m in list(self.markets.active.values()):
            if m.open_ts > now:
                continue  # next window tracked early; its candle hasn't opened yet
            if m.open_price is None:
                m.open_price = await self.binance.kline_open(m.interval, m.open_ts)
                if m.open_price is None:
                    continue
            drift = 0.0
            if r_recent is not None:
                drift = mom_beta * r_recent * min(m.t_remaining, mom_window) / mom_window
                # t_remaining goes negative for a few seconds after a window
                # closes, until the market manager sweeps it into `expired`
                clamp = mom_clamp * self.binance.vol_per_sec * math.sqrt(max(m.t_remaining, 0.0))
                drift = max(-clamp, min(clamp, drift))
            model_p = prob_up(
                spot, m.open_price, self.binance.vol_per_sec, m.t_remaining,
                tail_weight=fv.get("tail_weight", 0.25),
                tail_scale=fv.get("tail_scale", 2.5),
                drift=drift,
            )
            p_up = blend_with_market(
                model_p, self._market_mid(m), m.t_remaining,
                base_model_weight=fv.get("blend_model_weight", 0.65),
            )
            # regime-robust P(up) bounds for the sniper's dual-beta gate
            betas = tuple(self.cfg.get("sniper", "robust_betas", default=[0.83, 1.36]))
            p_lo, p_hi = prob_up_bounds(
                spot, m.open_price, self.binance.vol_per_sec, m.t_remaining,
                betas=betas,
                tail_weight=fv.get("tail_weight", 0.25),
                tail_scale=fv.get("tail_scale", 2.5),
            )
            marks[m.slug] = p_up
            self._model_marks[m.slug] = model_p
            await self._market_make(m, p_up)
            await self._snipe(m, p_up, p_lo, p_hi)
            await self._scalp(m, p_up)

        await self._settle_expired()

        equity = self.portfolio.equity(marks)
        if self._session_start_equity is None:
            self._session_start_equity = equity
            log.info("session baseline equity $%.2f (kill switch is relative to this)", equity)
        kill = self.cfg.get("risk", "kill_switch_loss_usd", default=200)
        if equity - self._session_start_equity < -kill:
            self._kill_count += 1
            log.error("KILL SWITCH: equity $%.2f, down $%.2f this session (limit $%.0f). "
                      "Cancelling all orders.",
                      equity, self._session_start_equity - equity, kill)
            for m in list(self.markets.active.values()):
                await self.exec.cancel_market(m)
            self.quotes.clear()
            self.portfolio.log_summary()
            if not self.paper:
                self.killed = True
                return
            # paper mode: the kill switch is a research event, not a stop.
            # Snapshot the summary above, rebase the loss limit on current
            # equity, and keep trading so the logs keep capturing data.
            self._session_start_equity = equity
            log.warning("paper mode: kill switch #%d this run — loss baseline "
                        "reset to $%.2f, continuing to trade", self._kill_count, equity)
            return

        now = time.time()
        if now - self._last_calib > 5:
            self._last_calib = now
            for m in self.markets.active.values():
                if m.slug in marks:
                    self._calib_write(
                        f"{int(now)},{m.kind},{m.slug},{m.t_remaining:.0f},"
                        f"{marks[m.slug]:.4f},{self._model_marks.get(m.slug, ''):.4f},")

        if now - self._last_status > 10:
            self._last_status = now
            vol_1m = self.binance.vol_per_sec * (60 ** 0.5) * 100
            fair_strs = [
                f"{m.kind}:{marks.get(m.slug, float('nan')):.2f}" for m in self.markets.active.values()
            ]
            extras = []
            if self.binance.perp_basis is not None:
                extras.append(f"perp basis {self.binance.perp_basis:+.1f}")
            if self.binance.coinbase_basis is not None:
                extras.append(f"cb basis {self.binance.coinbase_basis:+.1f}")
            suffix = f" ({', '.join(extras)})" if extras else ""
            log.info(
                "spot %.1f%s | vol(1m) %.3f%% | markets %d [%s] | cash $%.2f | equity $%.2f | exposure $%.2f | open orders %d",
                spot, suffix,
                vol_1m, len(self.markets.active), " ".join(fair_strs),
                self.portfolio.cash, equity, self.portfolio.exposure(),
                len(getattr(self.exec, "open_orders", {})),
            )
