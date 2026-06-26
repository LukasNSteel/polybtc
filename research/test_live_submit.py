"""Behavioural tests for the LiveExecutor taker submit path (P0/P1 changes).

Confirms, without touching the real CLOB:
  1. taker build + POST run on the reserved `clob-submit` pool, NOT the shared
     asyncio.to_thread executor (so a fire is never queued behind keep-warm).
  2. the shadow attempt gets the post-latency split: build/post/dispatch/call/
     resume, with call_ms tracking the real round-trip cost.
  3. a successful FAK fill is recorded (portfolio + fak_stats) and the orderID
     returned.
  4. a post_order that RAISES still stamps timing (finally-block done) and is
     booked as a kill, returning None.
  5. a no-match response (status unmatched) books a kill.
  6. run_keepwarm now defaults to a 1s cadence (P1).

Run: .venv/bin/python research/test_live_submit.py   (exit 0 = all pass)
"""

import asyncio
import inspect
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.execution import FakStats, LiveExecutor, Portfolio  # noqa: E402
from bot.markets import Market  # noqa: E402


class FakeClient:
    """Stand-in for py_clob_client_v2.ClobClient. Records the thread each call
    ran on and lets a test inject a POST latency / response / exception."""

    def __init__(self, resp=None, post_sleep=0.0, raise_on_post=False):
        self.resp = resp if resp is not None else {"status": "matched", "orderID": "0xfill"}
        self.post_sleep = post_sleep
        self.raise_on_post = raise_on_post
        self.build_thread = None
        self.post_thread = None

    def create_market_order(self, args, opts):
        self.build_thread = threading.current_thread().name
        return "SIGNED"

    def post_order(self, signed, order_type):
        self.post_thread = threading.current_thread().name
        if self.post_sleep:
            time.sleep(self.post_sleep)
        if self.raise_on_post:
            raise RuntimeError("simulated CLOB 400")
        return self.resp


class FakeShadow:
    """Returns a live dict from on_submit (the same object place_buy stamps
    timing onto) and captures the final record passed to on_result."""

    def __init__(self):
        self.attempt = None
        self.result = None

    def on_submit(self, market, outcome, token, limit_px, shares, leg, extra=None):
        self.attempt = {"_t0": time.monotonic(), "extra": dict(extra or {})}
        return self.attempt

    def on_result(self, attempt, filled_shares, avg_fill_px, status):
        self.result = {"attempt": attempt, "filled_shares": filled_shares,
                       "avg_fill_px": avg_fill_px, "status": str(status)}


def make_market():
    now = time.time()
    return Market(
        slug="btc-updown-test", title="BTC Test", condition_id="0xcid",
        token_up="UP", token_down="DN", open_ts=now - 60,
        close_ts=now + 120, tick=0.001, kind="5m", interval=300,
    )


def make_executor(client, shadow, presign=False):
    """Build a LiveExecutor without its network-heavy __init__."""
    ex = object.__new__(LiveExecutor)
    ex.portfolio = Portfolio(starting_cash=1000.0)
    ex.shadow = shadow
    ex.client = client
    ex.fak_stats = FakStats()
    ex.signature_type = 0
    ex._presign_enabled = presign
    ex._last_warm = time.monotonic()
    import concurrent.futures
    ex._submit_pool = concurrent.futures.ThreadPoolExecutor(
        max_workers=2, thread_name_prefix="clob-submit")
    if presign:
        # skip the real build/sign: hand back a sentinel signed order
        ex._take_presigned = lambda token, price, amount: ("SIGNED", amount)
    return ex


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        raise AssertionError(name)


async def test_fill_on_submit_pool():
    print("test_fill_on_submit_pool")
    client = FakeClient(resp={"status": "matched", "orderID": "0xfill"}, post_sleep=0.05)
    shadow = FakeShadow()
    ex = make_executor(client, shadow, presign=False)
    m = make_market()
    oid = await ex.place_buy(m, "up", 0.55, 10.0, leg="snipe")

    check("returns orderID", oid == "0xfill")
    check("build ran on submit pool", str(client.build_thread).startswith("clob-submit"))
    check("post ran on submit pool", str(client.post_thread).startswith("clob-submit"))
    check("position booked", ex.portfolio.pos(m.slug).up >= 10.0)
    check("fak fill recorded", ex.fak_stats.fills == 1 and ex.fak_stats.recent_fills == 1)

    a = shadow.attempt
    for f in ("build_ms", "post_ms", "dispatch_ms", "call_ms", "resume_ms"):
        check(f"timing field {f} present", a.get(f) is not None)
    check("call_ms ~tracks 50ms post sleep", 40.0 <= a["call_ms"] <= 400.0)
    check("dispatch_ms small (reserved pool)", a["dispatch_ms"] <= 200.0)
    check("post_ms >= call_ms", a["post_ms"] >= a["call_ms"] - 1.0)


async def test_presigned_path_skips_build():
    print("test_presigned_path_skips_build")
    client = FakeClient(resp={"status": "matched", "orderID": "0xps"}, post_sleep=0.0)
    shadow = FakeShadow()
    ex = make_executor(client, shadow, presign=True)
    m = make_market()
    oid = await ex.place_buy(m, "dn", 0.6, 9.0, leg="snipe")
    check("returns orderID", oid == "0xps")
    check("no build call (presigned)", client.build_thread is None)
    check("post ran on submit pool", str(client.post_thread).startswith("clob-submit"))
    check("presigned flag stamped", shadow.attempt.get("presigned") is True)


async def test_rejected_post_stamps_timing():
    print("test_rejected_post_stamps_timing")
    client = FakeClient(post_sleep=0.02, raise_on_post=True)
    shadow = FakeShadow()
    ex = make_executor(client, shadow, presign=False)
    m = make_market()
    oid = await ex.place_buy(m, "up", 0.55, 10.0, leg="snipe")
    check("returns None on reject", oid is None)
    check("kill recorded", ex.fak_stats.kills == 1 and ex.fak_stats.recent_fills == 0)
    check("on_result got rejected status", "rejected" in shadow.result["status"])
    # finally-block in _post sets 'done' even when post_order raises
    check("call_ms still measured on raise", shadow.attempt.get("call_ms") is not None)


async def test_no_match_books_kill():
    print("test_no_match_books_kill")
    client = FakeClient(resp={"status": "unmatched"}, post_sleep=0.0)
    shadow = FakeShadow()
    ex = make_executor(client, shadow, presign=False)
    m = make_market()
    await ex.place_buy(m, "up", 0.55, 10.0, leg="snipe")
    check("no position booked", ex.portfolio.pos(m.slug).up == 0.0)
    check("status recorded", shadow.result["status"] == "unmatched")


def test_keepwarm_default_1s():
    print("test_keepwarm_default_1s")
    sig = inspect.signature(LiveExecutor.run_keepwarm)
    default = sig.parameters["interval_sec"].default
    check("run_keepwarm default == 1.0", default == 1.0)


async def main():
    await test_fill_on_submit_pool()
    await test_presigned_path_skips_build()
    await test_rejected_post_stamps_timing()
    await test_no_match_books_kill()
    test_keepwarm_default_1s()
    print("\nALL PASS")


if __name__ == "__main__":
    asyncio.run(main())
