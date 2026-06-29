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
        self._last_reconcile = 0.0
        # live settlement: per-market last resolution-poll time, so a not-yet-
        # resolved expired market isn't hammered against the CLOB every tick
        self._resolve_checks: dict[str, float] = {}
        self._fak_tier = 0  # 0=ok, 1=soft adjust, 2=hard adjust, 3=paused
        self._fak_adjustments: dict = {
            "size_mult": 1.0, "min_edge_bump": 0.0,
            "snipe_cooldown_sec": 10, "scalp_cooldown_sec": 15,
        }
        self._scaling_reverted = False
        self._size_caps_cache: dict | None = None
        self._guard_was_allowed = True  # detect jump-guard trips for an instant batch yank
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
        # favourite-only enforcement, checked on the REALIZED fill (not just the
        # displayed ask the snipe gate saw). A snipe filling below min_ask means
        # the book collapsed during order latency and we landed on the underdog
        # — a bet against the favourite. The marketable FAK can't be unwound, so
        # this is a loud, counted alert; tighten close_buffer_sec/max_book_age_sec
        # if it recurs.
        if taker and leg == "snipe":
            min_ask = self.cfg.get("sniper", "min_ask", default=0.50)
            if price < min_ask:
                log.warning(
                    "ADVERSE SNIPE FILL %s %s: filled @ %.3f, BELOW favourite "
                    "floor (min_ask %.2f) — book collapsed in-flight and the FAK "
                    "landed on the underdog. This is a bet against the market "
                    "favourite; tighten sniper.close_buffer_sec / max_book_age_sec.",
                    market.title, outcome.upper(), price, min_ask)

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
        if self._size_caps_cache and "max_exposure_usd" in self._size_caps_cache:
            cap = self._size_caps_cache["max_exposure_usd"]
        else:
            cap = self.cfg.get("risk", "max_total_exposure_usd", default=500)
        return self.portfolio.exposure() + extra_usd <= cap

    def _equity_scale(self, equity: float) -> tuple[float, str]:
        """Bidirectional scale: grows with profits, shrinks on drawdown."""
        s = self.cfg.get("sizing", default={}) or {}
        ref = s.get("reference_equity") or self.portfolio.start_cash
        min_scale = s.get("min_scale", 0.5)
        mode = s.get("mode", "sqrt")
        if self._scaling_reverted:
            return 1.0, f"{mode}-reverted"

        if mode == "step":
            tiers = s.get("equity_tiers", [2500, 5000, 10000])
            mults = s.get("tier_multipliers", [1.0, 1.25, 1.5, 2.0])
            scale = mults[0]
            for i, tier in enumerate(tiers):
                if equity >= tier:
                    scale = mults[min(i + 1, len(mults) - 1)]
            if equity < ref:
                scale = min(scale, math.sqrt(max(equity, 1) / ref))
        else:
            scale = math.sqrt(max(equity, 1) / ref)
        return max(min_scale, scale), mode

    def _exposure_cap(self, scale: float) -> float:
        risk = self.cfg.get("risk", default={}) or {}
        base = risk.get("max_total_exposure_usd", 500)
        s = self.cfg.get("sizing", default={}) or {}
        if not s.get("enabled", False) or not s.get("scale_exposure", True):
            return base
        ceiling = risk.get("max_total_exposure_ceiling", base)
        return min(base * scale, ceiling)

    def _kill_limit(self) -> float:
        """Session drawdown stop: proportional to start equity, floored and capped."""
        risk = self.cfg.get("risk", default={}) or {}
        floor_usd = risk.get("kill_switch_loss_usd", 300)
        frac = risk.get("kill_switch_loss_frac", 0.30)
        cap_usd = risk.get("kill_switch_loss_cap_usd", 500)
        if self._session_start_equity is None:
            return floor_usd
        pct_limit = self._session_start_equity * frac
        return min(max(pct_limit, floor_usd), cap_usd)

    def _size_caps(self, equity: float) -> dict:
        """Scale per-trade and exposure caps with equity (sqrt or step-ups)."""
        sn = self.cfg.get("sniper", default={}) or {}
        sc = self.cfg.get("scalper", default={}) or {}
        base = {
            "max_take_usd": sn.get("max_take_usd", 100),
            "max_position_usd": sn.get("max_position_usd", 150),
            "max_scalp_usd": sc.get("max_usd", 100),
            "scale": 1.0,
            "mode": "fixed",
            "max_exposure_usd": self.cfg.get("risk", "max_total_exposure_usd", default=500),
        }
        s = self.cfg.get("sizing", default={}) or {}
        if not s.get("enabled", False):
            return base

        scale, mode = self._equity_scale(equity)
        take_ceil = s.get("max_take_usd_cap", 500)
        pos_ceil = s.get("max_position_usd_cap", 500)
        fak_mult = self._fak_adjustments.get("size_mult", 1.0)
        eff_scale = scale * fak_mult

        return {
            "max_take_usd": min(base["max_take_usd"] * eff_scale, take_ceil),
            "max_position_usd": min(base["max_position_usd"] * eff_scale, pos_ceil),
            "max_scalp_usd": min(base["max_scalp_usd"] * eff_scale, take_ceil),
            "max_exposure_usd": self._exposure_cap(eff_scale),
            "scale": scale,
            "fak_mult": fak_mult,
            "fak_tier": self._fak_tier,
            "mode": mode,
        }

    def _fak_tier_for_rate(self, rate: float, mode: str, fm: dict) -> int:
        soft = fm.get("min_fill_rate", 0.50)
        hard = fm.get("hard_fill_rate", 0.40)
        pause_at = fm.get("pause_fill_rate", 0.30)
        if rate >= soft:
            return 0
        if mode == "pause":
            return 3
        if mode == "adjust":
            return 2 if rate < hard else 1
        # graduated: soft adjust → hard adjust → pause
        if rate < pause_at:
            return 3
        if rate < hard:
            return 2
        return 1

    def _fak_adjustments_for_tier(self, tier: int, rate: float, fm: dict) -> dict:
        soft = fm.get("min_fill_rate", 0.50)
        bump = fm.get("min_edge_bump", 0.02)
        hard_mult = fm.get("hard_size_mult", 0.5)
        snipe_cd = fm.get("snipe_cooldown_sec", 10)
        hard_snipe_cd = fm.get("hard_snipe_cooldown_sec", 20)
        scalp_cd = fm.get("scalp_cooldown_sec", 15)
        if tier == 0:
            return {
                "size_mult": 1.0, "min_edge_bump": 0.0,
                "snipe_cooldown_sec": snipe_cd, "scalp_cooldown_sec": scalp_cd,
            }
        if tier == 1:
            return {
                "size_mult": rate / soft,
                "min_edge_bump": bump,
                "snipe_cooldown_sec": snipe_cd, "scalp_cooldown_sec": scalp_cd,
            }
        if tier == 2:
            return {
                "size_mult": hard_mult,
                "min_edge_bump": bump * 2,
                "snipe_cooldown_sec": hard_snipe_cd,
                "scalp_cooldown_sec": scalp_cd * 2,
            }
        return {
            "size_mult": 0.0, "min_edge_bump": 0.0,
            "snipe_cooldown_sec": snipe_cd, "scalp_cooldown_sec": scalp_cd,
        }

    def _log_fak_tier_change(self, old: int, new: int, rate: float,
                             fak, fm: dict) -> None:
        roll_n = fak.recent_count
        mode = fm.get("mode", "graduated")
        labels = {0: "normal", 1: "soft adjust", 2: "hard adjust", 3: "paused"}
        if new > old:
            level = log.error if new >= 3 else log.warning
            action = {
                1: f"revert scaling, size {rate / fm.get('min_fill_rate', 0.50):.2f}x, "
                   f"min_edge +{fm.get('min_edge_bump', 0.02):.2f}",
                2: f"size {fm.get('hard_size_mult', 0.5):.2f}x, min_edge +"
                   f"{fm.get('min_edge_bump', 0.02) * 2:.2f}, slower snipes",
                3: "pausing taker legs",
            }.get(new, "")
            level(
                "FAK STRESS tier %d→%d (%s): rolling %.0f%% over %d "
                "(session %d/%d) — %s",
                old, new, labels[new], rate * 100, roll_n,
                fak.fills, fak.attempts, action,
            )
        else:
            log.warning(
                "FAK RECOVERED tier %d→%d (%s): rolling %.0f%% over %d — %s",
                old, new, labels[new], rate * 100, roll_n,
                "restoring equity scaling" if new == 0 and (
                    fm.get("revert_scaling_on_stress",
                            fm.get("revert_scaling_on_pause", True))) else "easing limits",
            )

    def _check_fak_monitor(self) -> None:
        fak = getattr(self.exec, "fak_stats", None)
        fm = self.cfg.get("fak_monitor", default={}) or {}
        if fak is None or not fm.get("enabled", True):
            return

        # Recovery valve: while paused, takers log no outcomes, so the rolling
        # window stays frozen on the bad streak that triggered the pause and can
        # never climb back to the resume threshold. After pause_cooldown_sec of
        # taker inactivity, forget the stale window and re-probe (half-open).
        if fak.decay_if_stale(fm.get("pause_cooldown_sec", 90)) and self._fak_tier >= 3:
            self._fak_tier = 2  # half-open: resume at reduced size to re-test
            self._scaling_reverted = True
            self._fak_adjustments = self._fak_adjustments_for_tier(2, 0.0, fm)
            log.warning("FAK pause cooldown elapsed (%.0fs idle) — half-open "
                        "probe: resuming takers at tier 2 (%.2fx) to re-test "
                        "the fill rate", fm.get("pause_cooldown_sec", 90),
                        self._fak_adjustments.get("size_mult", 0.5))

        roll = fak.rolling_fill_rate()
        if roll is None:
            return

        mode = fm.get("mode", "graduated")
        revert = fm.get("revert_scaling_on_stress",
                         fm.get("revert_scaling_on_pause", True))
        new_tier = self._fak_tier_for_rate(roll, mode, fm)
        old_tier = self._fak_tier

        if new_tier != old_tier:
            self._fak_tier = new_tier
            if new_tier >= 1 and revert:
                self._scaling_reverted = True
            elif new_tier == 0:
                self._scaling_reverted = False
            self._log_fak_tier_change(old_tier, new_tier, roll, fak, fm)

        self._fak_adjustments = self._fak_adjustments_for_tier(self._fak_tier, roll, fm)

    def _position_cost(self, slug: str) -> float:
        p = self.portfolio.positions.get(slug)
        return p.cost if p else 0.0

    def _cutoff_sec(self, m: Market) -> float:
        key = {"5m": "cutoff_sec_5m", "15m": "cutoff_sec_15m",
               "4h": "cutoff_sec_4h"}.get(m.kind, "cutoff_sec_hourly")
        return self.cfg.get("trading", key, default=0)

    def _max_t_rem_sec(self, m: Market) -> float:
        """Per-kind late-window gate: only snipe inside the final N seconds of a
        window, where N depends on the market kind. A kind-specific key
        (max_t_rem_sec_{5m,15m,hourly,4h}) overrides the global sniper.max_t_rem_sec
        when set; 0/unset = full window. Per-kind windows are fitted on real
        15m/1h/4h data in research/test_longmkts_windows.py — the parquet replay
        archive is 5m-only, so the legacy global gate was a 5m result applied
        blindly to every kind (a 4h snipe could fire ~4h before resolution, where
        the EV is break-even-to-negative under realistic spreads)."""
        key = {"5m": "max_t_rem_sec_5m", "15m": "max_t_rem_sec_15m",
               "4h": "max_t_rem_sec_4h"}.get(m.kind, "max_t_rem_sec_hourly")
        per_kind = self.cfg.get("sniper", key, default=None)
        if per_kind is not None:
            return per_kind
        return self.cfg.get("sniper", "max_t_rem_sec", default=0) or 0

    def _max_t_rem_sec_far(self, m: Market) -> float:
        """High-conviction FAR-BAND ceiling (2026-06-29). Extends the snipe
        window from the inner max_t_rem_sec out to this value, but the extra band
        (max_t_rem_sec, max_t_rem_sec_far] only FIRES when distance-to-strike
        clears the stricter dist_sigma_min_far floor (see _dist_sigma_floor). The
        live shadow record showed UP snipes in the (90,170]s band are +EV ONLY for
        strong favourites (dσ>=1.0: 83-88% win, +12-16c/sh) — at dσ<1.0 the band is
        -EV, matching the forensic warning that a blanket widen recaptures losers.
        So we widen with a conviction gate, not for everyone. Per-kind
        max_t_rem_sec_{kind}_far overrides the global; 0/unset == NO far band
        (identical to the legacy single-window behaviour). Should be >= the inner
        max_t_rem_sec to have any effect."""
        key = {"5m": "max_t_rem_sec_5m_far", "15m": "max_t_rem_sec_15m_far",
               "4h": "max_t_rem_sec_4h_far"}.get(m.kind, "max_t_rem_sec_hourly_far")
        per_kind = self.cfg.get("sniper", key, default=None)
        if per_kind is not None:
            return per_kind
        return self.cfg.get("sniper", "max_t_rem_sec_far", default=0) or 0

    def _dist_sigma_floor(self, m: Market, t_remaining: float) -> float | None:
        """Distance-to-strike floor that applies at this t_remaining. The inner
        window [.., max_t_rem_sec] uses sniper.dist_sigma_min (the 13-week-validated
        0.7 sweet spot — see the config note). The extended far band
        (max_t_rem_sec, max_t_rem_sec_far], which sits further from settlement and
        reverts more, requires the stricter sniper.dist_sigma_min_far (strong
        favourites only). dist_sigma is already horizon-normalised, so the same
        number means '1σ of the move still to come' at either end of the window.
        Returns None when no floor is configured. Falls back to the inner floor if
        the far floor is unset, so a far band without its own floor degrades to a
        plain widened window (config-only choice, not the recommended one)."""
        inner = self.cfg.get("sniper", "dist_sigma_min", default=None)
        max_t_rem = self._max_t_rem_sec(m)
        far_floor = self.cfg.get("sniper", "dist_sigma_min_far", default=None)
        if max_t_rem and t_remaining > max_t_rem and far_floor is not None:
            return far_floor
        return inner

    def _close_buffer_sec(self, m: Market) -> float:
        """Settlement-safety cutoff: refuse to snipe inside the final N seconds
        before a window closes. This is NOT the late-window EDGE gate
        (max/min_t_rem_sec) — it is a separate guard against the settlement gap.

        In the last few seconds the order book gaps as the outcome resolves. A
        marketable FAK sized/priced off the book the strategy *saw* then lands
        ~100ms+ later against a book that has already collapsed, so it fills the
        losing side far BELOW the quoted ask — i.e. on the underdog the
        favourite-only `min_ask` gate is meant to exclude. The gate reads the
        pre-collapse displayed ask and passes; the fill is adverse. (Live
        session 1782200216: UP sniped on a 0.50 quote filled 0.35 ~3s before
        close, DN on a 0.73 quote filled 0.19 ~1s before close; both settled
        against us.) The favourite-only rule can only hold if we never take into
        the settlement gap. Mirrors the scalper's min_tau_sec resolution buffer.

        Per-kind key close_buffer_sec_{5m,15m,hourly,4h} overrides the global
        close_buffer_sec; 0/unset = no buffer (legacy behaviour)."""
        key = {"5m": "close_buffer_sec_5m", "15m": "close_buffer_sec_15m",
               "4h": "close_buffer_sec_4h"}.get(m.kind, "close_buffer_sec_hourly")
        per_kind = self.cfg.get("sniper", key, default=None)
        if per_kind is not None:
            return per_kind
        return self.cfg.get("sniper", "close_buffer_sec", default=0) or 0

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

    def _distance_to_strike(self, m: Market, side: str) -> tuple[float | None, float | None]:
        """Signed distance from the candle open to current spot, toward `side`,
        as (sigma, dollars). sigma = log(spot/open)/(vol*sqrt(t_rem)) — the same
        `d` that drives prob_up in fair_value. Positive = spot has already moved
        the bet's way. Returns (None, None) if any input is missing. Pure
        observation: nothing in the trading path reads this."""
        spot = self.binance.price
        openp = m.open_price
        vps = self.binance.vol_per_sec
        tr = max(m.t_remaining, 1e-9)
        if not spot or not openp or not vps or vps <= 0:
            return None, None
        d = math.log(spot / openp) / (vps * math.sqrt(tr))
        sgn = 1.0 if side == "up" else -1.0
        return round(sgn * d, 3), round(sgn * (spot - openp), 2)

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

    async def _snipe(self, m: Market, p_up: float, p_lo: float, p_hi: float,
                     caps: dict) -> None:
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
        if (not c["enabled"] or not self._tradable(m) or not self._vol_warm()
                or self._fak_tier >= 3):
            return
        # late-window gate (optional): only snipe inside [min_t_rem_sec,
        # max_t_rem_sec] before resolution. Backtest over ~8wk
        # (research/test_late_window.py): at the $1k/$250 bankrolls the early
        # window's fills carry a peak-to-trough drawdown LARGER than the deployed
        # capital (103% at $1k, 294% at $250), while the final ~60s concentrate
        # the win rate (64%->70%+) and the best ROI/$ at a fraction of that
        # drawdown. $1000 tier -> last 60s, $250 tier -> last 30s. Unset/0 ==
        # trade the full window (legacy behavior).
        # The far band (max_t_rem_sec_far) widens the ceiling for high-conviction
        # fires; the extra (max_t_rem_sec, max_t_rem_sec_far] band is admitted by
        # the window gate here but is then held to the stricter dist_sigma_min_far
        # floor below (via _dist_sigma_floor), so only strong favourites fire that
        # far out. Unset far ceiling -> effective_max == max_t_rem (legacy).
        max_t_rem = self._max_t_rem_sec(m)
        effective_max = max(max_t_rem, self._max_t_rem_sec_far(m))
        if effective_max and m.t_remaining > effective_max:
            return
        min_t_rem = c.get("min_t_rem_sec")
        if min_t_rem and m.t_remaining < min_t_rem:
            return
        # settlement-safety cutoff: never take into the final-seconds book gap,
        # where a FAK fills the collapsing side below the quoted ask (i.e. the
        # underdog the min_ask favourite gate is meant to exclude). See
        # _close_buffer_sec. Composes with min_t_rem_sec: effective floor is the
        # larger of the two.
        close_buffer = self._close_buffer_sec(m)
        if close_buffer and m.t_remaining < close_buffer:
            if self._cooled(m.slug, "snipe-close-buffer", "all", 30):
                log.info("SNIPE SKIP %s: %.0fs to close < %.0fs buffer — book "
                         "gaps into settlement; a FAK here fills the collapsing "
                         "side below the quoted ask (against the favourite)",
                         m.title, m.t_remaining, close_buffer)
            return
        per_mkt_cap = caps["max_position_usd"]
        min_edge = c["min_edge"] + self._fak_adjustments.get("min_edge_bump", 0.0)
        snipe_cd = self._fak_adjustments.get("snipe_cooldown_sec", 10)
        # tolerance band on the FAK limit: the signal ask with zero slack is
        # rejected by any single uptick during the speed-bump hold, even when
        # our fair richened too and the trade is still +EV. A couple ticks of
        # slack recaptures those (research/analyze_taker_widen.py: ~+10pp fill
        # rate at ~1.3c/share, all +EV) without adding adverse selection (the
        # gate only fires on favourable moves; adverse fills happen regardless).
        slack = c.get("limit_slack_ticks", 0) * m.tick
        imbalance = self._inventory_imbalance(m, p_up, cap=per_mkt_cap)
        max_inv = c.get("max_inventory_frac", 0.25)
        pos = self.portfolio.positions.get(m.slug)
        # TREND FILTER (mirror of the MM leg). Recent momentum in sigma units,
        # computed ONCE per call from already-collected Binance ticks — pure CPU,
        # no I/O, and it runs strictly BEFORE place_buy, so it can only PREVENT a
        # fire, never delay the order POST (the latency-critical path is
        # untouched). trend_z is logged on every fire (SNIPE line + shadow extra)
        # so the threshold can be tuned from the live shadow record. Live
        # forensics: fading a momentum run was the dominant snipe loss driver;
        # avoiding fades flipped the 5m book strongly +EV (research/
        # backtest_window_trend.py). 0 == filter off (still logged).
        trend_z = self._momentum_z(c.get("trend_window_sec", 45.0)) or 0.0
        trend_sigma = c.get("trend_filter_sigma", 0.0)
        # SIDE GATE (sniper.snipe_sides; unset/empty == both sides, legacy). When
        # set (e.g. ["up"]) only the listed sides place REAL fires; the rest are
        # routed to _shadow_candidates (reason 'side') so we keep a live outcome
        # record for the paused side. 2026-06-27: DN paused as PRECAUTIONARY risk
        # control, NOT a proven edge — live (49 fills) leans DN-negative but only
        # ~1.5σ (n.s.), while the 13-wk replay says DN is fine/better and STABLE,
        # and 5m Up base rate is 50.45% (no structural UP edge). The real unknown
        # is a possible recent regime the ~5-wk-old archive can't see; UP-only is
        # still +EV so this is cheap insurance. Re-enable -> [up, dn] once the
        # live/shadow DN record is flat-or-+EV over a meaningful sample. See
        # research/{deep_ev_analysis,backtest_sides,backtest_recency}.py + config.
        snipe_sides = c.get("snipe_sides")  # e.g. ["up"]; None == both
        for side, fair, robust, token in (("up", p_up, p_lo, m.token_up),
                                          ("dn", 1 - p_up, 1 - p_hi, m.token_down)):
            if snipe_sides and side not in snipe_sides:
                continue  # disabled side: no real fire (shadow-logged instead)
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
            # the favourite gate (min_ask) reads the displayed ask, so it is
            # only meaningful on a CURRENT book. A stale snapshot is how a
            # pre-collapse 0.50/0.73 ask passes the gate while the live book is
            # already deep underdog. Hold the sniper to a tight freshness window
            # (default 3s, was an implicit 15s); the close buffer covers the
            # sub-second gap the freshness check can't see.
            if not self._book_fresh(token, c.get("max_book_age_sec", 15.0)):
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
            if net_edge > min_edge:
                # distance-to-strike at fire time: how far spot has moved in this
                # bet's favour, in sigma of the remaining-horizon move (dist_sigma)
                # and in dollars (dist_usd). Always logged into the shadow record.
                dist_sigma, dist_usd = self._distance_to_strike(m, side)
                # SOFT DISTANCE FLOOR (band-aware). Inner window uses
                # sniper.dist_sigma_min (0.7, the 13-wk sweet spot); the far band
                # (beyond max_t_rem_sec) uses the stricter dist_sigma_min_far (1.0)
                # because further from settlement only strong favourites are +EV.
                # Live review of 21 dsig-logged fills: <0.5σ won 33% (-$6.05),
                # >=0.5σ won 72% (+$15.18); the shadow far-band record: dσ<1.0 was
                # -EV but dσ>=1.0 was 83-88% / +12-16c. Fail-open: never block when
                # dσ can't be computed (missing open/vol), so a stale signal can't
                # halt trading.
                dist_floor = self._dist_sigma_floor(m, m.t_remaining)
                if dist_floor and dist_sigma is not None and dist_sigma < dist_floor:
                    if self._cooled(m.slug, "snipe-dist", side, 30):
                        log.info("SNIPE SKIP %s %s: dσ %.2f < floor %.2f "
                                 "(t_rem %.0fs) — below the conviction floor for "
                                 "this window band",
                                 m.title, side.upper(), dist_sigma, dist_floor,
                                 m.t_remaining)
                    continue
                # TREND FILTER gate. Skip bets that FADE a momentum run beyond the
                # threshold: a BUY UP into a strong down-run, or a BUY DN into a
                # strong up-run. Conservative default — start wide and tune down
                # from the shadow trend_z distribution. Fail-open (trend_z=0 when
                # momentum is unavailable, so a missing signal never halts trading).
                trending_against = ((side == "up" and trend_z <= -trend_sigma)
                                    or (side == "dn" and trend_z >= trend_sigma))
                if trend_sigma > 0 and trending_against:
                    if self._cooled(m.slug, "snipe-trend", side, 30):
                        log.info("SNIPE SKIP %s %s: momentum %.2fσ against the bet "
                                 "(trend_filter %.2f) — not fading the run",
                                 m.title, side.upper(), trend_z, trend_sigma)
                    continue
                if c.get("flat_size", False):
                    # winner's curse: a bigger modeled edge correlates with model
                    # ERROR, not opportunity (edge>=0.15 went ~0-for-7 live), so do
                    # NOT scale stake with edge — that put the most capital on the
                    # worst bets. Flat fraction of max_take within the kept band,
                    # still capped by the displayed book size.
                    usd = min(caps["max_take_usd"] * c.get("size_frac", 0.5),
                              ask_px * ask_sz)
                else:
                    # size proportional to conviction: full size at 2x min_edge
                    conviction = min(1.0, net_edge / (2 * min_edge))
                    usd = min(caps["max_take_usd"] * max(0.25, conviction), ask_px * ask_sz)
                shares = max(usd / ask_px, MIN_SHARES)
                if (self._exposure_ok(usd)
                        and self._position_cost(m.slug) + usd <= per_mkt_cap
                        and self._cooled(m.slug, "snipe", side, snipe_cd)):
                    limit_px = (round_tick(min(ask_px + slack, 1.0 - m.tick), m.tick)
                                if slack else ask_px)
                    log.info("SNIPE %s %s: ask %.3f (limit %.3f) + fee %.4f vs "
                             "robust %.3f (blend %.3f, edge %.3f, $%.0f, dσ %s, "
                             "tz %.2f)",
                             m.title, side.upper(), ask_px, limit_px, fee, robust,
                             fair, net_edge, usd,
                             f"{dist_sigma:.2f}" if dist_sigma is not None else "na",
                             trend_z)
                    await self.exec.place_buy(
                        m, side, limit_px, shares, leg="snipe",
                        extra={"dist_sigma": dist_sigma, "dist_usd": dist_usd,
                               "trend_z": round(trend_z, 3)})

    def _shadow_candidates(self, m: Market, p_up: float, p_lo: float,
                           p_hi: float) -> None:
        """OBSERVATION-ONLY counterfactual logger — places NO orders and never
        touches the submit path. Called AFTER _snipe returns, so it cannot delay a
        real fire; for markets in the shadow-only band there is no real order at
        all. Pure CPU (book lookups + arithmetic) plus, at most once per
        (market,side) per minute, one small JSONL append.

        It re-evaluates the snipe SIGNAL gates (favourite ask, robust edge,
        distance floor — read from the SAME config keys as _snipe so thresholds
        can't drift) across a WIDER 'shadow' window than the live timing gate, and
        records any would-be fire the live config DECLINED:
          * reason 'window' — the signal qualified but t_remaining is outside the
            live max_t_rem_sec band (the trades a wider window would recapture);
          * reason 'trend'  — inside the live window but the trend filter blocked
            it (the trades a looser trend_filter_sigma would recapture).
        Each record carries trend_z + title/side/ts so it can be joined to the
        SETTLE outcome offline (research/analyze_shadow_candidates.py), letting us
        calibrate max_t_rem_sec / trend_filter_sigma on real data at zero risk.

        Trades the live config WOULD have taken (in-window AND trend-OK) are NOT
        logged here — those are real attempts already in shadow_taker.jsonl."""
        shadow = getattr(self.exec, "shadow", None)
        c = self.cfg["sniper"]
        if shadow is None or not c.get("shadow_candidates", False) or not c["enabled"]:
            return
        key = {"5m": "shadow_max_t_rem_sec_5m", "15m": "shadow_max_t_rem_sec_15m",
               "4h": "shadow_max_t_rem_sec_4h"}.get(
                   m.kind, "shadow_max_t_rem_sec_hourly")
        shadow_max = c.get(key, c.get("shadow_max_t_rem_sec", 0)) or 0
        if not shadow_max:
            return  # no shadow band configured for this kind
        trem = m.t_remaining
        close_buffer = self._close_buffer_sec(m)
        if trem > shadow_max or trem < close_buffer:
            return  # outside the shadow band entirely (cheap early out)
        live_max = self._max_t_rem_sec(m)
        far_max = self._max_t_rem_sec_far(m)
        far_floor = c.get("dist_sigma_min_far")
        min_t_rem = c.get("min_t_rem_sec") or 0
        floor_lo = max(close_buffer, min_t_rem)
        # inner [.., live_max] band fires regardless of the far floor; the far
        # (live_max, far_max] band fires live ONLY for dσ>=far_floor (decided
        # per-side below once dist_sigma is known). Trades that now fire live are
        # NOT logged here (they land in shadow_taker.jsonl as real attempts); the
        # sub-threshold far band IS still logged so we keep observing it.
        in_inner = (not live_max or trem <= live_max) and trem >= floor_lo
        trend_z = self._momentum_z(c.get("trend_window_sec", 45.0)) or 0.0
        trend_sigma = c.get("trend_filter_sigma", 0.0)
        min_edge = c["min_edge"]
        max_edge = c.get("max_edge", 0.25)
        snipe_sides = c.get("snipe_sides")  # disabled sides -> reason 'side'
        for side, fair, robust, token in (("up", p_up, p_lo, m.token_up),
                                          ("dn", 1 - p_up, 1 - p_hi, m.token_down)):
            if not self._book_fresh(token, c.get("max_book_age_sec", 15.0)):
                continue
            book = self.feed.books.get(token)
            ask = book.best_ask() if book else None
            if not ask:
                continue
            ask_px, ask_sz = ask
            if ask_px < c.get("min_ask", 0.50) or ask_px > c.get("max_ask", 0.80):
                continue
            fee = m.taker_fee_per_share(ask_px)
            net_edge = robust - ask_px - fee
            if not (min_edge < net_edge <= max_edge):
                continue
            dist_sigma, dist_usd = self._distance_to_strike(m, side)
            # observe the whole wide band at the INNER floor (0.7) so we keep
            # eyes on the sub-conviction far slice; whether it FIRES live is
            # decided by in_far (which applies the stricter far floor).
            dist_floor = c.get("dist_sigma_min")
            if dist_floor and dist_sigma is not None and dist_sigma < dist_floor:
                continue
            in_far = bool(far_max and live_max and live_max < trem <= far_max
                          and trem >= floor_lo
                          and (far_floor is None
                               or (dist_sigma is not None and dist_sigma >= far_floor)))
            in_live_window = in_inner or in_far
            trending_against = ((side == "up" and trend_z <= -trend_sigma)
                                or (side == "dn" and trend_z >= trend_sigma))
            trend_block = trend_sigma > 0 and trending_against
            side_disabled = bool(snipe_sides) and side not in snipe_sides
            # log ONLY counterfactuals (trades we did NOT take live); the rest are
            # real fires already recorded in shadow_taker.jsonl. A disabled side
            # (snipe_sides) is a counterfactual too — log it (reason 'side') so we
            # keep a live outcome record for the side we paused.
            if in_live_window and not trend_block and not side_disabled:
                continue
            if not self._cooled(m.slug, "shadow-cand", side, 60):
                continue
            snap = shadow.snapshot(token)
            shadow.log_candidate({
                "slug": m.slug, "title": m.title, "kind": m.kind, "side": side,
                "leg": "snipe", "t_remaining_s": round(trem, 1),
                "seen_ask_px": snap["ask_px"], "seen_ask_sz": snap["ask_sz"],
                "seen_mid": snap["mid"], "book_age_ms": snap["book_age_ms"],
                "robust": round(robust, 4), "fair": round(fair, 4),
                "net_edge": round(net_edge, 4),
                "dist_sigma": round(dist_sigma, 3) if dist_sigma is not None else None,
                "dist_usd": round(dist_usd, 2) if dist_usd is not None else None,
                "trend_z": round(trend_z, 3), "trend_sigma": trend_sigma,
                "trend_block": trend_block, "in_live_window": in_live_window,
                "side_disabled": side_disabled,
                "reason": ("side" if side_disabled
                           else "trend" if (in_live_window and trend_block)
                           else "window"),
            })

    async def _scalp(self, m: Market, p_up: float, caps: dict) -> None:
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
                or self._fak_tier >= 3
                or m.t_remaining > c["window_sec"] or m.t_remaining <= min_tau):
            return
        scalp_cd = self._fak_adjustments.get("scalp_cooldown_sec", 15)
        # FAK limit slack (see _snipe). Capped at max_price so a probe never
        # pays past the scalper's tested near-certainty band.
        slack = c.get("limit_slack_ticks", 0) * m.tick
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
            usd = min(caps["max_scalp_usd"], ask[0] * ask[1])
            shares = max(usd / ask[0], MIN_SHARES)
            if self._exposure_ok(usd) and self._cooled(m.slug, "scalp", side, scalp_cd):
                limit_px = (round_tick(min(ask[0] + slack, c["max_price"]), m.tick)
                            if slack else ask[0])
                log.info("SCALP %s %s @ %.3f (limit %.3f, fair %.4f, %.0fs left)",
                         m.title, side.upper(), ask[0], limit_px, fair, m.t_remaining)
                await self.exec.place_buy(m, side, limit_px, shares, leg="scalp")

    # ---------- settlement ----------

    async def _market_outcome(self, m: Market) -> bool | None:
        """True if Up won, False if Down, None if not resolvable yet.

        LIVE: the REAL Polymarket resolution (Chainlink/UMA via the CLOB/Gamma
        APIs) — never the Binance kline, which disagrees on coin-flip closes
        (basis risk) and would book the wrong winner into real P&L.
        PAPER: the Binance close-vs-open proxy is the simulator's own truth."""
        if not self.paper:
            return await self.markets.resolved_outcome(m)
        if m.open_price is None:  # e.g. position restored after a restart
            m.open_price = await self.binance.kline_open(m.interval, m.open_ts)
        close = await self.binance.kline_close(m.interval, m.open_ts)
        if close is None or m.open_price is None:
            return None
        return close >= m.open_price

    async def _settle_expired(self) -> None:
        still_waiting = []
        now = time.time()
        poll_sec = self.cfg.get("live", "resolve_poll_sec", default=10)
        for m in self.markets.expired:
            # live: real resolution lags the close (oracle), so poll each
            # waiting market at most every poll_sec instead of every 0.25s tick
            if not self.paper:
                if now - self._resolve_checks.get(m.slug, 0.0) < poll_sec:
                    still_waiting.append(m)
                    continue
                self._resolve_checks[m.slug] = now
            up_won = await self._market_outcome(m)
            if up_won is None:
                # keep retrying long enough for restored positions to resolve
                if now - m.close_ts < 24 * 3600:
                    still_waiting.append(m)
                continue
            self.portfolio.settle(m, up_won)
            self._calib_write(f"{int(time.time())},{m.kind},{m.slug},0,,,{1 if up_won else 0}")
            if hasattr(self.exec, "queue_redeem"):
                self.exec.queue_redeem(m)
            self.quotes.pop(m.slug, None)
            self.fill_guards.drop_market(m.slug)
            self._resolve_checks.pop(m.slug, None)
        self.markets.expired = still_waiting

    async def _reconcile_wallet(self, now: float, marks: dict) -> None:
        """LIVE: periodically compare the bot's internal cash against the REAL
        wallet pUSD collateral. Settlement now books the real Polymarket
        resolution, so the two should track closely; a large/growing gap means
        the books are drifting from reality (a settlement or redemption bug)
        and is surfaced loudly here. Read off-thread + throttled so it never
        sits on the strategy hot path. Pure observability — does not trade."""
        if self.paper or not hasattr(self.exec, "collateral_balance"):
            return
        if now - self._last_reconcile < self.cfg.get("live", "reconcile_sec", default=30):
            return
        self._last_reconcile = now
        try:
            bal = await asyncio.to_thread(self.exec.collateral_balance)
        except Exception as e:  # noqa: BLE001 — a balance read must never stop trading
            log.warning("reconcile: wallet collateral read failed: %s", e)
            return
        if not bal or bal <= 0:
            return
        open_val = sum(p.up * marks.get(s, 0.5) + p.dn * (1 - marks.get(s, 0.5))
                       for s, p in self.portfolio.positions.items())
        div = self.portfolio.cash - bal
        warn_usd = self.cfg.get("live", "reconcile_warn_usd", default=10.0)
        emit = log.warning if abs(div) > warn_usd else log.info
        emit("RECONCILE bot cash $%.2f vs wallet collateral $%.2f (divergence "
             "%+.2f) | open positions $%.2f", self.portfolio.cash, bal, div, open_val)

    # ---------- main loop ----------

    async def run(self, tick_sec: float = 0.25) -> None:
        while True:
            await asyncio.sleep(tick_sec)
            try:
                await self._step()
            except Exception:
                log.exception("strategy step failed")

    async def _pull_all_quotes(self) -> None:
        ids = [state.pop(side)[0] for state in self.quotes.values() for side in list(state)]
        if ids:
            await self.exec.cancel_orders(ids)  # single round trip yanks the whole book

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
        # the instant the guard trips (BTC swing), yank every resting quote in
        # one batch call rather than waiting for each market's _market_make to
        # pull them side-by-side over the next ticks
        guard_ok = self.jump_guard.allowed(now)
        if self._guard_was_allowed and not guard_ok and self.quotes:
            await self._pull_all_quotes()
        self._guard_was_allowed = guard_ok
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
        mark_rows: list[tuple[Market, float, float, float]] = []
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
            mark_rows.append((m, p_up, p_lo, p_hi))

        equity = self.portfolio.equity(marks)
        self._check_fak_monitor()
        caps = self._size_caps(equity)
        self._size_caps_cache = caps

        for m, p_up, p_lo, p_hi in mark_rows:
            await self._market_make(m, p_up)
            await self._snipe(m, p_up, p_lo, p_hi, caps)
            # observation-only: log would-be snipes a wider window / looser trend
            # filter would take (no orders, off the fire path) — runs last so it
            # can never delay a real fire. No-op unless sniper.shadow_candidates.
            self._shadow_candidates(m, p_up, p_lo, p_hi)
            await self._scalp(m, p_up, caps)

        await self._settle_expired()
        equity = self.portfolio.equity(marks)
        if self._session_start_equity is None:
            self._session_start_equity = equity
            log.info("session baseline equity $%.2f (kill switch %.0f%% capped $%.0f–$%.0f)",
                     equity,
                     100 * self.cfg.get("risk", "kill_switch_loss_frac", default=0.30),
                     self.cfg.get("risk", "kill_switch_loss_usd", default=300),
                     self.cfg.get("risk", "kill_switch_loss_cap_usd", default=500))
        kill = self._kill_limit()
        if equity - self._session_start_equity < -kill:
            self._kill_count += 1
            log.error("KILL SWITCH: equity $%.2f, down $%.2f this session (limit $%.0f). "
                      "Cancelling all orders.",
                      equity, self._session_start_equity - equity, kill)
            await self.exec.cancel_all()  # yank everything in a single call
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
        await self._reconcile_wallet(now, marks)
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
            cap_bits = []
            if self._size_caps_cache:
                c = self._size_caps_cache
                cap_bits.append(f"scale {c['scale']:.2f}x ({c['mode']})")
                if c.get("fak_mult", 1.0) != 1.0:
                    cap_bits.append(f"fak {c['fak_mult']:.2f}x")
                cap_bits.append(f"take ${c['max_take_usd']:.0f}")
                cap_bits.append(f"exp ${c['max_exposure_usd']:.0f}")
            fak = getattr(self.exec, "fak_stats", None)
            if fak and fak.attempts:
                roll = fak.rolling_fill_rate()
                if roll is not None:
                    tier = self._fak_tier
                    tier_str = f" t{tier}" if tier else ""
                    cap_bits.append(f"FAK {100 * roll:.0f}%/{fak.recent_count}{tier_str}")
                else:
                    cap_bits.append(f"FAK {100 * fak.fills / fak.attempts:.0f}%")
            if self._fak_tier >= 3:
                cap_bits.append("takers PAUSED")
            elif self._fak_tier >= 1:
                cap_bits.append(f"takers ADJUST t{self._fak_tier}")
            cap_str = f" | {' | '.join(cap_bits)}" if cap_bits else ""
            log.info(
                "spot %.1f%s | vol(1m) %.3f%% | markets %d [%s] | cash $%.2f | equity $%.2f | exposure $%.2f | open orders %d%s",
                spot, suffix,
                vol_1m, len(self.markets.active), " ".join(fair_strs),
                self.portfolio.cash, equity, self.portfolio.exposure(),
                len(getattr(self.exec, "open_orders", {})),
                cap_str,
            )
