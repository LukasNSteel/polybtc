"""Tests for the order-book self-refresh (bot/orderbook.apply_rest_book +
LiveExecutor.run_book_refresh). Verifies: REST snapshots are parsed (dict and
object form), book.ts is stamped fresh, the refresh loop touches ONLY stale
tokens, respects the per-cycle cap, and is a no-op when disabled (period<=0)."""
import asyncio
import sys
import time
import types

sys.path.insert(0, __file__.rsplit("/research/", 1)[0])

from bot.orderbook import OrderBookFeed


def test_apply_rest_book_dict_and_object():
    feed = OrderBookFeed()
    ok = feed.apply_rest_book("tokA", {"bids": [{"price": "0.49", "size": "10"}],
                                       "asks": [{"price": "0.51", "size": "8"},
                                                {"price": "0.52", "size": "3"}]})
    assert ok
    b = feed.books["tokA"]
    assert b.best_ask() == (0.51, 8.0)
    assert b.best_bid() == (0.49, 10.0)
    assert time.time() - b.ts < 1.0

    class Lvl:
        def __init__(self, p, s): self.price, self.size = p, s

    class OB:
        bids = [Lvl("0.60", "5")]
        asks = [Lvl("0.62", "4")]
    assert feed.apply_rest_book("tokB", OB())
    assert feed.books["tokB"].best_ask() == (0.62, 4.0)
    # empty snapshot -> no-op, not applied
    assert not feed.apply_rest_book("tokC", {"bids": [], "asks": []})


class FakeClient:
    def __init__(self):
        self.fetched = []

    def get_order_book(self, token):
        self.fetched.append(token)
        return {"bids": [{"price": "0.49", "size": "10"}],
                "asks": [{"price": "0.51", "size": "9"}]}


def _exec_with(client):
    from bot.execution import LiveExecutor
    ex = LiveExecutor.__new__(LiveExecutor)   # skip __init__ (no network)
    ex.client = client
    return ex


def test_refresh_touches_only_stale_and_caps():
    feed = OrderBookFeed()
    client = FakeClient()
    ex = _exec_with(client)
    # 3 markets x 2 tokens = 6 tokens; mark two FRESH (recent ts), rest stale.
    mk = []
    for i in range(3):
        up, dn = f"u{i}", f"d{i}"
        mk.append(types.SimpleNamespace(token_up=up, token_down=dn))
        feed.books[up]; feed.books[dn]              # create empty (ts=0 -> stale)
    feed.books["u0"].ts = time.time() + 100          # unambiguously fresh -> skip
    feed.books["d0"].ts = time.time() + 100          # unambiguously fresh -> skip

    async def run_one():                             # let exactly ONE cycle fire
        task = asyncio.create_task(
            ex.run_book_refresh(feed, lambda: mk, period=0.05, max_per_cycle=3))
        await asyncio.sleep(0.09)
        task.cancel()
    asyncio.run(run_one())

    # fresh tokens never fetched; only stale ones; per-cycle cap (3) respected
    assert "u0" not in client.fetched and "d0" not in client.fetched
    assert all(t in {"u1", "d1", "u2", "d2"} for t in client.fetched)
    assert len(client.fetched) == 3


def test_disabled_is_noop():
    feed = OrderBookFeed()
    client = FakeClient()
    ex = _exec_with(client)
    mk = [types.SimpleNamespace(token_up="x", token_down="y")]

    async def run_one():
        # period<=0 must return immediately and fetch nothing
        await asyncio.wait_for(
            ex.run_book_refresh(feed, lambda: mk, period=0.0), timeout=1.0)
    asyncio.run(run_one())
    assert client.fetched == []


if __name__ == "__main__":
    test_apply_rest_book_dict_and_object()
    test_refresh_touches_only_stale_and_caps()
    test_disabled_is_noop()
    print("all book-refresh tests passed")
