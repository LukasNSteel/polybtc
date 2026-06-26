"""Isolated POST-latency probe (zero cost).

Measures the py_clob_client_v2 post_order round trip from a QUIET process (no
asyncio loop, no WS firehose) to separate two causes of the ~310ms call_ms the
live bot sees on every FAK:

  #1 GIL/event-loop contention DURING the call  -> isolated POST would be FAST (~80ms)
  #3 genuine server-side /order round trip       -> isolated POST stays ~310ms

It posts a deliberately UNMARKETABLE FAK BUY (limit far below the ask) so the
venue kills it with "no orders found to match": no fill, no position, no spend.
Uses the same warm httpx client the bot installs, and warms the socket first so
we measure a hot POST (apples-to-apples with the bot's keep-warm path).

Run on the box:  .venv/bin/python research/post_latency_probe.py
"""

import os
import statistics as st
import time
from pathlib import Path

import yaml
from dotenv import load_dotenv

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
load_dotenv()

from bot.execution import _install_warm_clob_http_client  # noqa: E402
from bot.live_trade_smoke import _best_ask, _candidate_markets  # noqa: E402

HOST = "https://clob.polymarket.com"
N = 10


def main():
    from py_clob_client_v2 import (
        ClobClient, MarketOrderArgs, OrderType, PartialCreateOrderOptions, Side,
    )

    key = os.environ["POLYMARKET_PRIVATE_KEY"]
    funder = os.environ.get("POLYMARKET_FUNDER") or None
    sig_type = int(yaml.safe_load(open("config.live165.yaml"))["live"]["signature_type"])
    kwargs = {"key": key, "chain_id": 137, "signature_type": sig_type}
    if funder:
        kwargs["funder"] = funder
    client = ClobClient(HOST, **kwargs)
    client.set_api_creds(client.create_or_derive_api_key())
    _install_warm_clob_http_client()  # match the bot's tuned client
    print(f"sig_type={sig_type} funder={'yes' if funder else 'no'}")

    # pick a market whose favorite ask is in a real range so 0.02 is safely
    # below it (guaranteed no match)
    chosen = None
    for mk in _candidate_markets():
        for token in (mk["token_up"], mk["token_down"]):
            ask = _best_ask(client, token)
            if ask and 0.40 <= ask[0] <= 0.90:
                chosen = (mk, token, ask)
                break
        if chosen:
            break
    if not chosen:
        raise SystemExit("no suitable market/book right now")
    mk, token, (ask_px, ask_sz) = chosen
    print(f"market: {mk['title']}  best_ask={ask_px} (sz {ask_sz})")
    client.get_clob_market_info(mk["condition_id"])  # warm market-info cache

    opts = PartialCreateOrderOptions(neg_risk=mk["neg_risk"])

    def build_unmarketable():
        # $1 (>= venue min size) buy @ 0.02 -> passes amount validation and
        # reaches the MATCHING engine, finds nothing at/below 0.02 vs a 0.4-0.9
        # ask -> "no orders found to match" kill. Same server path as the bot's
        # missed FAKs, but zero fill => zero cost / no position.
        return client.create_market_order(
            MarketOrderArgs(token_id=token, amount=1.0, side=Side.BUY,
                            price=0.02, order_type=OrderType.FAK), opts)

    def timed_post():
        signed = build_unmarketable()
        t0 = time.perf_counter()
        try:
            client.post_order(signed, OrderType.FAK)
            status = "matched?!"  # should never happen at 0.02
        except Exception as e:  # noqa: BLE001 — a 400 'no match' is the expected kill
            status = "killed" if "no orders found to match" in str(e) else f"err:{str(e)[:40]}"
        return (time.perf_counter() - t0) * 1000, status

    # warm the socket (TLS/HTTP2) so we measure a HOT post, like the bot's keepwarm
    for _ in range(3):
        try:
            client.get_server_time()
        except Exception:  # noqa: BLE001
            pass
    timed_post()  # discard first

    samples, statuses = [], {}
    for _ in range(N):
        ms, status = timed_post()
        samples.append(ms)
        statuses[status] = statuses.get(status, 0) + 1
        time.sleep(0.5)

    samples.sort()
    print(f"\nISOLATED post_order (n={len(samples)}, quiet process, warm socket):")
    print(f"  median {st.median(samples):.0f}ms  min {min(samples):.0f}ms  "
          f"p90 {samples[int(len(samples)*0.9)-1]:.0f}ms  max {max(samples):.0f}ms")
    print(f"  statuses: {statuses}")
    print("\nbot in-loop call_ms ~= 310ms. If the above is ~310ms => SERVER-bound "
          "(GIL is not the cause). If ~80-120ms => the bot's 310ms is event-loop/"
          "GIL contention during the call.")


if __name__ == "__main__":
    main()
