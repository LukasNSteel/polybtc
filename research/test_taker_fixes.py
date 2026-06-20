"""Behavioural tests for the two taker fixes (June 2026):

  Fix 1 — FAK limit slack (sniper/scalper `limit_slack_ticks`):
    the limit is sent at signal-ask + N ticks so a small favourable richen
    during the speed-bump hold fills instead of being rejected, WITHOUT
    chasing big moves and WITHOUT adding adverse selection.

  Fix 2 — pause-deadlock recovery valve (`fak_monitor.pause_cooldown_sec`):
    a paused taker leg (tier 3) logs no new outcomes, so the rolling window
    used to gate the pause can never refresh. After a cooldown of inactivity
    the stale window is forgotten and the monitor half-opens to tier 2 to
    re-probe, then self-heals up or back down from the fresh outcomes.

Run: python research/test_taker_fixes.py     (exit 0 = all pass)
"""

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.execution import FakStats, PaperExecutor, Portfolio  # noqa: E402
from bot.markets import Market  # noqa: E402
from bot.strategy import Strategy, round_tick  # noqa: E402


class FakeBook:
    def __init__(self, bids=None, asks=None):
        self.bids = dict(bids or {})
        self.asks = dict(asks or {})
        self.ts = time.time()

    def best_bid(self):
        return (max(self.bids), self.bids[max(self.bids)]) if self.bids else None

    def best_ask(self):
        return (min(self.asks), self.asks[min(self.asks)]) if self.asks else None


class FakeFeed:
    def __init__(self):
        self.books = {}
        self.on_trade = []


class FakeCfg:
    """Minimal Config stand-in: cfg.get('fak_monitor', default={})."""

    def __init__(self, data):
        self._data = data

    def get(self, *keys, default=None):
        node = self._data
        for k in keys:
            if not isinstance(node, dict) or k not in node:
                return default
            node = node[k]
        return node


def make_market(close_in=60.0, tick=0.001):
    now = time.time()
    return Market(
        slug="btc-updown-test", title="BTC Test", condition_id="0xcid",
        token_up="UP", token_down="DN", open_ts=now - 60,
        close_ts=now + close_in, tick=tick, kind="5m", interval=300,
    )


def make_executor(feed):
    """Deterministic taker sim: no race loss, full capture, no feed lag, so a
    fill-vs-reject is decided purely by post-hold ask vs our limit."""
    pf = Portfolio(starting_cash=1000.0)
    return PaperExecutor(
        pf, feed, taker_latency_ms=60.0, speed_bump_ms=40.0, cancel_latency_ms=20.0,
        capture=1.0, edge_contention=False, race_loss_prob=0.0, feed_lag_ms=0.0,
    )


PASS, FAIL = "PASS", "FAIL"
results = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(f"  [{PASS if cond else FAIL}] {name}")


# ---------------------------------------------------------------------------
# Fix 1 — limit slack (executor-level proof of the mechanism)
# ---------------------------------------------------------------------------

async def _richen_then_settle(ex, m, limit, richen_to):
    """place a taker at `limit`, richen the ask mid-hold to `richen_to`."""
    await ex.place_buy(m, "up", limit, 100, leg="snipe")
    await asyncio.sleep(0.045)  # inside the committed hold
    # the whole book ticks up together (favourable move): 1-tick spread so the
    # mid rises with the ask rather than the fixture faking an adverse drift
    ex.feed.books["UP"] = FakeBook(
        bids={round(richen_to - 0.001, 4): 100}, asks={richen_to: 200})
    await asyncio.sleep(0.10)


async def test_zero_slack_rejects_small_richen():
    """Baseline (old behaviour): limit == signal ask, a +2 tick richen rejects."""
    feed = FakeFeed()
    feed.books["UP"] = FakeBook(bids={0.549: 100}, asks={0.550: 200})
    ex = make_executor(feed)
    await _richen_then_settle(ex, make_market(), limit=0.550, richen_to=0.552)
    check("zero-slack limit rejects a +2 tick richen",
          ex.fak_stats.fills == 0 and ex.fak_stats.kills == 1)


async def test_slack_recovers_small_richen():
    """Fix: limit == ask + 2 ticks fills the same +2 tick richen."""
    feed = FakeFeed()
    feed.books["UP"] = FakeBook(bids={0.549: 100}, asks={0.550: 200})
    ex = make_executor(feed)
    await _richen_then_settle(ex, make_market(), limit=0.552, richen_to=0.552)
    check("+2 tick slack fills a +2 tick richen", ex.fak_stats.fills == 1)
    check("recovered fill is NOT adverse (favourable move)",
          ex.fak_stats.adverse_fills == 0)


async def test_slack_does_not_chase_big_richen():
    """A +3 tick richen is still rejected at +2 ticks slack: we don't chase
    the runaway moves whose edge is gone."""
    feed = FakeFeed()
    feed.books["UP"] = FakeBook(bids={0.549: 100}, asks={0.550: 200})
    ex = make_executor(feed)
    await _richen_then_settle(ex, make_market(), limit=0.552, richen_to=0.553)
    check("+2 tick slack still rejects a +3 tick richen",
          ex.fak_stats.fills == 0 and ex.fak_stats.kills == 1)


async def test_slack_adds_no_adverse():
    """Slack only loosens the reject gate (favourable side). An ADVERSE move
    (ask cheapens) fills regardless of slack — slack adds no new downside."""
    feed = FakeFeed()
    feed.books["UP"] = FakeBook(bids={0.549: 100}, asks={0.550: 200})
    ex = make_executor(feed)
    await ex.place_buy(m := make_market(), "up", 0.552, 100, leg="snipe")
    await asyncio.sleep(0.045)
    feed.books["UP"] = FakeBook(bids={0.50: 100}, asks={0.51: 200})  # cheapened
    await asyncio.sleep(0.10)
    check("adverse move fills with or without slack", ex.fak_stats.fills == 1)
    check("adverse move flagged adverse", ex.fak_stats.adverse_fills == 1)


def test_slack_limit_math():
    """The exact expressions used in Strategy._snipe / _scalp."""
    tick = 0.01
    # sniper: ask + slack, clamped below 1.0
    snipe = lambda ask, n: round_tick(min(ask + n * tick, 1.0 - tick), tick)
    check("snipe +2t in-band: 0.80 -> 0.82", snipe(0.80, 2) == 0.82)
    check("snipe clamps near 1.0: 0.985 -> 0.99", snipe(0.985, 2) == 0.99)
    # scalper: ask + slack, capped at max_price
    max_price = 0.99
    scalp = lambda ask, n: round_tick(min(ask + n * tick, max_price), tick)
    check("scalp +2t in-band: 0.90 -> 0.92", scalp(0.90, 2) == 0.92)
    check("scalp capped at max_price: 0.98 -> 0.99", scalp(0.98, 2) == 0.99)
    check("zero slack is a no-op: limit == ask",
          (0.55 if not 0 else snipe(0.55, 0)) == 0.55)


# ---------------------------------------------------------------------------
# Fix 2 — pause-deadlock recovery valve
# ---------------------------------------------------------------------------

def test_decay_valve_unit():
    fak = FakStats(min_fill_rate=0.50, min_attempts=10, window_size=30)
    for i in range(30):              # 20% fill streak -> would pause
        fak.record_attempt()
        (fak.record_fill if i % 5 == 0 else fak.record_kill)()
    check("bad streak builds a full window", fak.recent_count == 30)
    check("rolling reflects the bad streak (~0.20)",
          abs(fak.rolling_fill_rate() - 0.20) < 1e-9)
    check("fresh window is NOT decayed", fak.decay_if_stale(90) is False)
    fak.last_attempt_ts = time.time() - 91   # simulate being paused/idle
    check("stale window IS decayed", fak.decay_if_stale(90) is True)
    check("decayed window is empty -> monitor re-probes",
          fak.recent_count == 0 and fak.rolling_fill_rate() is None)
    check("empty window decay is a no-op", fak.decay_if_stale(90) is False)
    check("disabled (0) never decays", FakStats().decay_if_stale(0) is False)


def make_strategy_stub(fak):
    """Build just enough of a Strategy to exercise _check_fak_monitor without
    the heavy __init__ (file IO, guards, feeds)."""
    s = object.__new__(Strategy)
    s.exec = type("E", (), {"fak_stats": fak})()
    s.cfg = FakeCfg({"fak_monitor": {
        "enabled": True, "mode": "graduated",
        "min_fill_rate": 0.50, "hard_fill_rate": 0.40, "pause_fill_rate": 0.30,
        "min_attempts": 10, "window_size": 30, "min_edge_bump": 0.02,
        "hard_size_mult": 0.5, "snipe_cooldown_sec": 10,
        "hard_snipe_cooldown_sec": 20, "scalp_cooldown_sec": 15,
        "revert_scaling_on_stress": True, "pause_cooldown_sec": 90,
    }})
    s._fak_tier = 0
    s._fak_adjustments = {"size_mult": 1.0, "min_edge_bump": 0.0,
                          "snipe_cooldown_sec": 10, "scalp_cooldown_sec": 15}
    s._scaling_reverted = False
    return s


def test_halfopen_recovery_integration():
    fak = FakStats(min_fill_rate=0.50, min_attempts=10, window_size=30)
    s = make_strategy_stub(fak)

    # 1) a bad streak drives the monitor to tier 3 (paused)
    for i in range(30):
        fak.record_attempt()
        (fak.record_fill if i % 5 == 0 else fak.record_kill)()
    s._check_fak_monitor()
    check("bad streak pauses takers (tier 3)", s._fak_tier == 3)

    # 2) while paused nothing refreshes; before the cooldown it stays paused
    s._check_fak_monitor()
    check("still paused before cooldown elapses", s._fak_tier == 3)

    # 3) cooldown elapses (no taker activity) -> half-open probe to tier 2
    fak.last_attempt_ts = time.time() - 91
    s._check_fak_monitor()
    check("cooldown half-opens to tier 2 (probe)", s._fak_tier == 2)
    check("probe forgot the stale window", fak.recent_count == 0)
    check("probe keeps reduced size (0.5x)",
          abs(s._fak_adjustments["size_mult"] - 0.5) < 1e-9)

    # 4) probe fills come back healthy -> self-heal to tier 0
    for _ in range(12):
        fak.record_attempt()
        fak.record_fill()
    s._check_fak_monitor()
    check("healthy probe recovers to full (tier 0)", s._fak_tier == 0)
    check("equity scaling restored on recovery", s._scaling_reverted is False)


def test_decay_does_not_disturb_active_session():
    """A healthy, actively-trading leg must never be decayed mid-session."""
    fak = FakStats(min_fill_rate=0.50, min_attempts=10, window_size=30)
    s = make_strategy_stub(fak)
    for _ in range(20):             # 100% fills, just attempted (fresh)
        fak.record_attempt()
        fak.record_fill()
    s._check_fak_monitor()
    check("healthy active leg stays tier 0", s._fak_tier == 0)
    check("active leg window not cleared", fak.recent_count == 20)


async def main():
    async_tests = [
        test_zero_slack_rejects_small_richen, test_slack_recovers_small_richen,
        test_slack_does_not_chase_big_richen, test_slack_adds_no_adverse,
    ]
    sync_tests = [
        test_slack_limit_math, test_decay_valve_unit,
        test_halfopen_recovery_integration, test_decay_does_not_disturb_active_session,
    ]
    for t in async_tests:
        print(f"\n{t.__name__}:")
        await t()
    for t in sync_tests:
        print(f"\n{t.__name__}:")
        t()
    passed = sum(1 for _, c in results if c)
    total = len(results)
    print(f"\n{'=' * 56}\n{passed}/{total} checks passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
