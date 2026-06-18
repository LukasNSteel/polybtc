"""Behavioural tests for the paper speed-bump executor.

Exercises the itode speed-bump model added to PaperExecutor:
  1. committed/uncancellable hold (consequence 1: no cancel escape)
  2. adverse-selection fill when the side cheapens during the hold
  3. rejection when the side richens (favourable move) during the hold
  4. rejection when liquidity vanishes during the hold
  5. window-closes-during-hold kill
  6. latency budget split (pre-bump jittered + fixed 250ms bump)

Run: python research/test_speed_bump_exec.py   (exit 0 = all pass)
"""

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.execution import PaperExecutor, Portfolio  # noqa: E402
from bot.markets import Market  # noqa: E402


class FakeBook:
    def __init__(self, bids=None, asks=None):
        self.bids = dict(bids or {})
        self.asks = dict(asks or {})
        self.ts = time.time()

    def best_bid(self):
        if not self.bids:
            return None
        p = max(self.bids)
        return p, self.bids[p]

    def best_ask(self):
        if not self.asks:
            return None
        p = min(self.asks)
        return p, self.asks[p]


class FakeFeed:
    def __init__(self):
        self.books = {}
        self.on_trade = []


def make_market(close_in=60.0):
    now = time.time()
    return Market(
        slug="btc-updown-test", title="BTC Test", condition_id="0xcid",
        token_up="UP", token_down="DN", open_ts=now - 60,
        close_ts=now + close_in, tick=0.001, kind="5m", interval=300,
    )


def make_executor(feed):
    pf = Portfolio(starting_cash=1000.0)
    # tiny latencies so tests run fast but keep the 2:1 pre-bump:bump ratio
    return PaperExecutor(pf, feed, taker_latency_ms=60.0, speed_bump_ms=40.0,
                         cancel_latency_ms=20.0)


PASS, FAIL = "PASS", "FAIL"
results = []


def check(name, cond):
    results.append((name, cond))
    print(f"  [{PASS if cond else FAIL}] {name}")


async def test_adverse_fill():
    """Side cheapens during the hold -> committed fill, counted adverse."""
    feed = FakeFeed()
    feed.books["UP"] = FakeBook(bids={0.54: 100}, asks={0.55: 200})
    ex = make_executor(feed)
    m = make_market()
    oid = await ex.place_buy(m, "up", 0.55, 100, leg="snipe")
    check("place_buy returns an order id for taker leg", oid is not None)
    # mid0 snapshot happens after the pre-bump sleep (~20ms). Move the book
    # down (side cheapened, adverse for a long) just before the bump ends.
    await asyncio.sleep(0.045)
    feed.books["UP"] = FakeBook(bids={0.50: 100}, asks={0.51: 200})
    await asyncio.sleep(0.10)
    check("adverse move fills (committed hold)", ex.fak_stats.fills == 1)
    check("adverse fill counted as adverse", ex.fak_stats.adverse_fills == 1)
    check("portfolio took an up position", ex.portfolio.pos(m.slug).up > 0)


async def test_favourable_reject():
    """Side richens during the hold -> cheap quote gone -> FAK rejected."""
    feed = FakeFeed()
    feed.books["UP"] = FakeBook(bids={0.54: 100}, asks={0.55: 200})
    ex = make_executor(feed)
    m = make_market()
    await ex.place_buy(m, "up", 0.55, 100, leg="snipe")
    await asyncio.sleep(0.045)
    # ask jumps above our 0.55 limit (BTC moved our way; faster takers got it)
    feed.books["UP"] = FakeBook(bids={0.59: 100}, asks={0.60: 200})
    await asyncio.sleep(0.10)
    check("favourable move rejected (no fill)", ex.fak_stats.fills == 0)
    check("favourable reject counted as kill", ex.fak_stats.kills == 1)
    check("not flagged adverse", ex.fak_stats.adverse_fills == 0)


async def test_liquidity_vanishes():
    """Ask disappears entirely during the hold -> rejected."""
    feed = FakeFeed()
    feed.books["UP"] = FakeBook(bids={0.54: 100}, asks={0.55: 200})
    ex = make_executor(feed)
    m = make_market()
    await ex.place_buy(m, "up", 0.55, 100, leg="snipe")
    await asyncio.sleep(0.045)
    feed.books["UP"] = FakeBook(bids={0.54: 100}, asks={})
    await asyncio.sleep(0.10)
    check("vanished liquidity rejected", ex.fak_stats.fills == 0
          and ex.fak_stats.kills == 1)


async def test_no_cancel_escape():
    """A cancel issued during the hold must be ignored, order still fills."""
    feed = FakeFeed()
    feed.books["UP"] = FakeBook(bids={0.54: 100}, asks={0.55: 200})
    ex = make_executor(feed)
    m = make_market()
    oid = await ex.place_buy(m, "up", 0.55, 100, leg="snipe")
    # wait until inside the bump (committed), then try to cancel
    await asyncio.sleep(0.045)
    check("order is committed during hold", oid in ex._committed)
    await ex.cancel(oid)
    await ex.cancel_market(m)
    await asyncio.sleep(0.10)
    check("cancel during hold did NOT prevent fill", ex.fak_stats.fills == 1)
    check("committed set cleared after hold", oid not in ex._committed)


async def test_window_closes_during_hold():
    """Window closing mid-hold kills the order (no fill)."""
    feed = FakeFeed()
    feed.books["UP"] = FakeBook(bids={0.54: 100}, asks={0.55: 200})
    ex = make_executor(feed)
    m = make_market(close_in=0.03)  # closes during the ~60ms total latency
    await ex.place_buy(m, "up", 0.55, 100, leg="snipe")
    await asyncio.sleep(0.12)
    check("window-close mid-hold -> kill, no fill",
          ex.fak_stats.fills == 0 and ex.fak_stats.kills == 1)


async def test_latency_budget():
    """Total fill time ~= pre-bump (jittered) + fixed bump, and the bump is
    a meaningful fraction of the budget."""
    feed = FakeFeed()
    feed.books["UP"] = FakeBook(bids={0.54: 100}, asks={0.55: 200})
    ex = make_executor(feed)
    m = make_market()
    t0 = time.time()
    await ex.place_buy(m, "up", 0.55, 100, leg="snipe")
    # poll until resolved (fill or kill)
    while ex.fak_stats.fills + ex.fak_stats.kills == 0:
        await asyncio.sleep(0.005)
    elapsed = time.time() - t0
    # pre_bump = 60-40 = 20ms jittered [10,30], + fixed 40ms bump => [50,70]ms
    check("total latency >= fixed bump (40ms)", elapsed >= 0.040)
    check("total latency within budget envelope", 0.045 <= elapsed <= 0.12)
    check("speed_bump is a subset of taker_latency",
          ex.speed_bump <= ex.taker_latency and ex.speed_bump == 0.040)


async def main():
    for t in (test_adverse_fill, test_favourable_reject, test_liquidity_vanishes,
              test_no_cancel_escape, test_window_closes_during_hold,
              test_latency_budget):
        print(f"\n{t.__name__}:")
        await t()
    passed = sum(1 for _, c in results if c)
    total = len(results)
    print(f"\n{'=' * 50}\n{passed}/{total} checks passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
