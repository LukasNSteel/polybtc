"""Adverse-selection defenses for the market-making leg.

A fair-value MM in fast crypto markets has one dominant failure mode: its
resting bids are the stale quotes during a BTC jump, picked off by snipers
(bots exactly like our own sniper leg) in the gap between the move and our
next cancel/replace cycle. Three layered defenses, plus direct measurement:

1. JumpGuard — when the Binance price moves more than `sigma` standard
   deviations (with an absolute floor) inside a short window, pull all MM
   quotes immediately and pause re-quoting for a cooldown. The EWMA vol
   estimate lags a burst, so during the jump the model is at its most
   overconfident — better to stand aside for seconds than quote into it.
2. FillGuards — quote fading + same-side fill breaker. Each MM fill on a
   side pushes that side's next quote further from fair (escalating, capped,
   decaying window): a fast informed trader can't clear us out repeatedly at
   one stale level. Too many same-side fills in a short window stops quoting
   that side entirely for a cooldown.
3. MarkoutTracker — measures adverse selection directly: for every fill,
   post-fill drift of the token's book mid at fixed horizons
   (markout = mid(t+h) − fill price; we only ever buy). Consistently
   negative MM markouts mean the flow hitting us is informed. Reported
   per leg in the session summary — it also scores the sniper (should be
   strongly positive) and the scalper.
"""

import logging
import math
import time
from collections import deque

log = logging.getLogger("guards")


class JumpGuard:
    def __init__(self, window_sec: float = 3.0, sigma: float = 5.0,
                 min_move: float = 5e-4, cooldown_sec: float = 10.0):
        self.window = window_sec
        self.sigma = sigma
        self.min_move = min_move
        self.cooldown = cooldown_sec
        self._px: deque[tuple[float, float]] = deque()
        self._paused_until = 0.0

    def allowed(self, now: float | None = None) -> bool:
        return (now or time.time()) >= self._paused_until

    def force_pause(self, now: float, duration: float, reason: str) -> None:
        """External trigger (e.g. liquidation burst) — pull MM quotes."""
        until = now + duration
        if until > self._paused_until:
            if self.allowed(now):
                log.warning("JUMP GUARD (external): %s — pulling MM quotes for %.0fs",
                            reason, duration)
            self._paused_until = until

    def update(self, price: float, vol_per_sec: float, now: float) -> bool:
        """Feed the latest price; returns False while MM should stand aside."""
        self._px.append((now, price))
        while self._px and self._px[0][0] < now - self.window:
            self._px.popleft()
        t0, p0 = self._px[0]
        dt = now - t0
        if dt > 0.05 and p0 > 0:
            move = abs(math.log(price / p0))
            threshold = max(self.sigma * vol_per_sec * math.sqrt(dt), self.min_move)
            if move >= threshold:
                if self.allowed(now):
                    log.warning("JUMP GUARD: %.1f bps in %.1fs (threshold %.1f) — "
                                "pulling MM quotes for %.0fs",
                                move * 1e4, dt, threshold * 1e4, self.cooldown)
                self._paused_until = now + self.cooldown
        return self.allowed(now)


class FillGuards:
    """Per-(market, side) quote fading and same-side fill breaker, driven by
    our own MM fills."""

    def __init__(self, fade_per_fill: float = 0.005, fade_window_sec: float = 60,
                 fade_max: float = 0.02, breaker_fills: int = 4,
                 breaker_window_sec: float = 60, breaker_cooldown_sec: float = 180):
        self.fade_per_fill = fade_per_fill
        self.fade_window = fade_window_sec
        self.fade_max = fade_max
        self.breaker_fills = breaker_fills
        self.breaker_window = breaker_window_sec
        self.breaker_cooldown = breaker_cooldown_sec
        self._fills: dict[tuple[str, str], deque[float]] = {}
        self._blocked_until: dict[tuple[str, str], float] = {}

    def record_fill(self, slug: str, side: str, now: float, title: str = "") -> None:
        key = (slug, side)
        fills = self._fills.setdefault(key, deque())
        fills.append(now)
        cutoff = now - self.breaker_window
        while fills and fills[0] < cutoff:
            fills.popleft()
        if len(fills) >= self.breaker_fills and now >= self._blocked_until.get(key, 0):
            log.warning("FILL BREAKER: %d %s MM fills in %.0fs on %s — "
                        "side off for %.0fs", len(fills), side.upper(),
                        self.breaker_window, title or slug, self.breaker_cooldown)
            self._blocked_until[key] = now + self.breaker_cooldown

    def blocked(self, slug: str, side: str, now: float) -> bool:
        return now < self._blocked_until.get((slug, side), 0)

    def fade(self, slug: str, side: str, now: float) -> float:
        """Extra edge (price units) to add to this side's quote."""
        fills = self._fills.get((slug, side))
        if not fills:
            return 0.0
        n = sum(1 for ts in fills if ts >= now - self.fade_window)
        return min(self.fade_max, n * self.fade_per_fill)

    def drop_market(self, slug: str) -> None:
        for key in [k for k in self._fills if k[0] == slug]:
            del self._fills[key]
        for key in [k for k in self._blocked_until if k[0] == slug]:
            del self._blocked_until[key]


class MarkoutTracker:
    """Post-fill book-mid drift at fixed horizons, aggregated per strategy leg."""

    def __init__(self, horizons_sec: tuple[float, ...] = (10.0, 60.0)):
        self.horizons = horizons_sec
        self._pending: list[dict] = []
        # leg -> horizon -> list of markouts (price units)
        self._stats: dict[str, dict[float, list[float]]] = {}

    def record_fill(self, token: str, price: float, leg: str, now: float) -> None:
        self._pending.append({"ts": now, "token": token, "price": price,
                              "leg": leg, "done": set()})

    def resolve(self, mid_lookup, now: float) -> None:
        """mid_lookup(token) -> mid | None (book gone => sample dropped)."""
        still = []
        for p in self._pending:
            for h in self.horizons:
                if h in p["done"] or now < p["ts"] + h:
                    continue
                p["done"].add(h)
                mid = mid_lookup(p["token"])
                if mid is None:
                    continue
                self._stats.setdefault(p["leg"], {}).setdefault(h, []).append(
                    mid - p["price"])
            if len(p["done"]) < len(self.horizons):
                still.append(p)
        self._pending = still

    def summary_lines(self) -> list[str]:
        lines = []
        for leg in sorted(self._stats):
            parts = []
            for h in self.horizons:
                vals = self._stats[leg].get(h)
                if vals:
                    parts.append(f"{h:.0f}s {sum(vals) / len(vals) * 100:+.2f}c (n={len(vals)})")
            if parts:
                lines.append(f"  markout {leg:<6} {'  '.join(parts)}")
        if lines:
            lines.append("  (markout = post-fill mid drift; negative MM markouts = "
                         "we are being picked off)")
        return lines
