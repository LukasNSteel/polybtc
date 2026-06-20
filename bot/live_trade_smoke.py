"""One-shot live trade smoke test for the polybtc EOA (signature_type 0).

Places a single marketable FAK BUY (~$1 by default) on a current BTC market to
confirm the whole live path end-to-end and RECORD the three things we care about:

  * LATENCY  — EIP-712 sign time (local, many samples) and the real order POST
               round trip, i.e. the controllable slice of tick-to-trade.
  * FEE      — the actual taker fee (modeled vs implied from the fill legs), to
               confirm/calibrate the paper bot's fee model (rate * p*(1-p) * sh).
  * FILL     — the raw response so we can see exactly what the venue returns.

DRY RUN by default: it discovers a market, fetches the best ask, and measures
sign latency, but does NOT place an order. Pass --fire to actually submit.

    python -m bot.live_trade_smoke              # dry run (no order placed)
    python -m bot.live_trade_smoke --fire       # place the real ~$1 order
    python -m bot.live_trade_smoke --fire --usd 1.0 --side favorite

Prereqs (LIVE): POLYMARKET_PRIVATE_KEY in .env, the EOA funded with pUSD
collateral + a little POL, and `python -m bot.setup_eoa` run once.
"""

import argparse
import json
import logging
import os
import statistics
import time

import requests
import truststore
from dotenv import load_dotenv

truststore.inject_into_ssl()
load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("trade_smoke")

GAMMA = "https://gamma-api.polymarket.com"
HOST = "https://clob.polymarket.com"
HOURLY_SERIES_ID = "10114"  # btc-up-or-down-hourly
MIN_SHARES = 5.0            # Polymarket minimum order size


def _candidate_markets() -> list[dict]:
    """All current BTC hourly markets accepting orders (newest first)."""
    r = requests.get(f"{GAMMA}/events",
                     params={"series_id": HOURLY_SERIES_ID, "active": "true",
                             "closed": "false", "limit": "20"}, timeout=10)
    r.raise_for_status()
    out = []
    for e in r.json():
        m = (e.get("markets") or [{}])[0]
        if not m.get("acceptingOrders", False):
            continue
        try:
            outcomes = json.loads(m["outcomes"])
            tokens = json.loads(m["clobTokenIds"])
        except (KeyError, json.JSONDecodeError):
            continue
        up_idx = outcomes.index("Up")
        fs = m.get("feeSchedule") or {}
        out.append({
            "slug": e.get("slug"), "title": e.get("title"),
            "condition_id": m.get("conditionId", ""),
            "token_up": tokens[up_idx], "token_down": tokens[1 - up_idx],
            "tick": float(m.get("orderPriceMinTickSize", 0.001)),
            "neg_risk": bool(m.get("negRisk", False)),
            "fees_enabled": bool(m.get("feesEnabled")),
            "fee_rate": float(fs.get("rate", 0.0)) if m.get("feesEnabled") else 0.0,
            "fee_exponent": float(fs.get("exponent", 1.0)),
            "fee_schedule": fs,
            "end": e.get("endDate") or m.get("endDate"),
        })
    if not out:
        raise SystemExit("no active hourly BTC market accepting orders right now")
    return out


def _best_ask(client, token: str) -> tuple[float, float] | None:
    ob = client.get_order_book(token)
    asks = (ob.get("asks") if isinstance(ob, dict) else getattr(ob, "asks", None)) or []
    levels = []
    for a in asks:
        try:
            p = float(a["price"] if isinstance(a, dict) else a.price)
            s = float(a["size"] if isinstance(a, dict) else a.size)
            levels.append((p, s))
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
    return min(levels, key=lambda x: x[0]) if levels else None


def main() -> None:
    ap = argparse.ArgumentParser(description="live $1 trade smoke + latency/fee recorder")
    ap.add_argument("--fire", action="store_true", help="actually place the order (default: dry run)")
    ap.add_argument("--usd", type=float, default=1.0, help="target stake in pUSD (default 1.0)")
    ap.add_argument("--side", choices=["favorite", "up", "dn"], default="favorite",
                    help="which side to buy (default: the favorite, ask in [0.50,0.80])")
    ap.add_argument("--slippage", type=float, default=0.02,
                    help="fraction above best ask for the marketable FAK limit (default 0.02)")
    ap.add_argument("--sign-samples", type=int, default=50, help="EIP-712 sign-latency samples")
    ap.add_argument("--sig-type", type=int, default=None,
                    help="signature_type (default: read live.signature_type from config.yaml)")
    args = ap.parse_args()

    from py_clob_client_v2 import (
        AssetType, BalanceAllowanceParams, ClobClient, MarketOrderArgs, OrderType,
        PartialCreateOrderOptions, Side,
    )
    from .execution import _parse_fill_price, _parse_fill_shares, _signing_backend

    key = os.environ.get("POLYMARKET_PRIVATE_KEY")
    funder = os.environ.get("POLYMARKET_FUNDER") or None
    if not key:
        raise SystemExit("set POLYMARKET_PRIVATE_KEY in .env first")

    sig_type = args.sig_type
    if sig_type is None:
        try:
            import yaml
            sig_type = int(yaml.safe_load(open("config.yaml"))["live"]["signature_type"])
        except Exception:  # noqa: BLE001
            sig_type = 0
    if sig_type in (1, 2, 3) and not funder:
        raise SystemExit(f"signature_type={sig_type} (proxy/deposit wallet) requires "
                         "POLYMARKET_FUNDER (your Polymarket deposit address) in .env")

    native, backend = _signing_backend()
    log.info("signing backend: %s (%s)", backend, "NATIVE/fast" if native else "PURE-PYTHON/SLOW")

    kwargs = {"key": key, "chain_id": 137, "signature_type": sig_type}
    if funder:
        kwargs["funder"] = funder
    client = ClobClient(HOST, **kwargs)
    client.set_api_creds(client.create_or_derive_api_key())
    log.info("signer: %s  | maker/funder: %s  | signature_type=%d",
             client.get_address(), funder or "(EOA, self)", sig_type)

    # deposit wallets (type 3) must sync on-chain balances into the CLOB cache or
    # orders are rejected with "not enough balance/allowance".
    bal_params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=sig_type)
    if sig_type in (1, 2, 3):
        try:
            client.update_balance_allowance(bal_params)
        except Exception as e:  # noqa: BLE001
            log.warning("balance sync failed: %s", e)
    try:
        log.info("collateral balance/allowance: %s", client.get_balance_allowance(bal_params))
    except Exception as e:  # noqa: BLE001
        log.warning("could not read collateral balance/allowance: %s", e)

    markets = _candidate_markets()
    log.info("scanning %d active hourly markets for a live two-sided book...", len(markets))

    # Find a (market, side, ask) where the favorite is in a real trading range
    # [0.50,0.80] — this naturally skips decided/one-sided windows whose asks
    # sit at 0.01/0.99. For --side up/dn we just need an ask on that side.
    def sides_for(mk):
        if args.side == "favorite":
            return [("up", mk["token_up"]), ("dn", mk["token_down"])]
        return [(args.side, mk["token_up"] if args.side == "up" else mk["token_down"])]

    chosen = None
    fallback = None
    for mk in markets:
        for side, token in sides_for(mk):
            ask = _best_ask(client, token)
            if not ask:
                continue
            if fallback is None:
                fallback = (mk, side, token, ask)
            in_range = (0.50 <= ask[0] <= 0.80) if args.side == "favorite" else (ask[0] < 0.99)
            if in_range:
                chosen = (mk, side, token, ask)
                break
        if chosen:
            break
    chosen = chosen or fallback
    if chosen is None:
        raise SystemExit("no asks on the book for any active market right now")

    mk, side, token, (ask_px, ask_sz) = chosen
    log.info("market: %s (%s) ends %s", mk["title"], mk["slug"], mk.get("end"))
    log.info("fee schedule (Gamma): enabled=%s rate=%s exp=%s raw=%s",
             mk["fees_enabled"], mk["fee_rate"], mk["fee_exponent"], mk["fee_schedule"])

    # pre-warm the CLOB market-info cache so the sign path is pure-CPU
    try:
        client.get_clob_market_info(mk["condition_id"])
    except Exception as e:  # noqa: BLE001
        log.warning("market-info prewarm failed: %s", e)

    import math
    tick = mk["tick"]
    ask_px = round(ask_px, 6)
    # Marketable limit: cross a little ABOVE the ask (rounded up to a tick) so the
    # FAK still fills if the fast-moving BTC book ticks up between book-read and
    # submit. Pricing exactly at the ask gets killed the instant the ask moves.
    cross_px = round(min(0.99, math.ceil(ask_px * (1 + args.slippage) / tick) * tick), 6)
    shares = round(max(MIN_SHARES, args.usd / ask_px), 2)  # >= venue 5-share floor
    if args.usd / ask_px < MIN_SHARES:
        log.warning("$%.2f is below the 5-share minimum at %.3f; bumping to %.0f sh",
                    args.usd, ask_px, MIN_SHARES)
    amount = round(shares * cross_px, 2)  # budget at the worst (limit) price
    log.info("plan: BUY %s %s  %.2f sh, limit %.3f (ask %.3f +%.1f%%) = up to $%.2f "
             "(book ask size %.0f)", mk["title"], side.upper(), shares, cross_px, ask_px,
             args.slippage * 100, amount, ask_sz)

    opts = PartialCreateOrderOptions(neg_risk=mk["neg_risk"])

    def build():
        return client.create_market_order(
            MarketOrderArgs(token_id=token, amount=amount, side=Side.BUY,
                            price=cross_px, order_type=OrderType.FAK), opts)

    # ---- sign latency (local, no order placed) ----
    build()  # warm
    samples = []
    for _ in range(max(5, args.sign_samples)):
        t0 = time.perf_counter()
        signed = build()
        samples.append((time.perf_counter() - t0) * 1000)
    log.info("SIGN LATENCY: median %.3f ms, p95 %.3f ms, min %.3f ms (n=%d, EIP-712 build+sign)",
             statistics.median(samples),
             sorted(samples)[int(0.95 * len(samples)) - 1], min(samples), len(samples))

    if not args.fire:
        log.info("DRY RUN — no order placed. Re-run with --fire to submit the ~$%.2f order.", amount)
        return

    # Ground-truth fee = the collateral actually debited, minus the fill cost.
    # V2 takes the taker fee out of BALANCE at match time (not the fill price),
    # so this is the ONLY reliable way to observe it — the fill-leg ratio can't.
    def _collateral() -> float | None:
        try:
            if sig_type in (1, 2, 3):
                client.update_balance_allowance(bal_params)  # resync cache from chain
            ba = client.get_balance_allowance(bal_params)
            return int(ba.get("balance", 0)) / 1e6
        except Exception as e:  # noqa: BLE001
            log.warning("balance read failed: %s", e)
            return None

    bal_before = _collateral()

    # ---- post latency (the real submission round trip) ----
    t0 = time.perf_counter()
    resp = client.post_order(signed, OrderType.FAK)
    post_ms = (time.perf_counter() - t0) * 1000
    log.info("POST LATENCY: %.1f ms (real order submission round trip from THIS host)", post_ms)
    log.info("raw response: %s", resp)

    filled = _parse_fill_shares(resp, shares)
    if filled <= 0:
        log.warning("no fill (FAK killed). status=%s — try again on a tighter spread.",
                    resp.get("status") if isinstance(resp, dict) else "?")
        return
    avg = _parse_fill_price(resp, ask_px)
    cost = avg * filled
    modeled_fee = mk["fee_rate"] * (avg * (1 - avg)) ** mk["fee_exponent"] * filled
    log.info("FILLED: %.2f sh @ %.4f = $%.2f", filled, avg, cost)

    # let the on-chain settlement propagate before re-reading the balance
    time.sleep(2.5)
    bal_after = _collateral()
    measured_fee = None
    if bal_before is not None and bal_after is not None:
        spent = bal_before - bal_after
        measured_fee = spent - cost

    log.info("FEE CHECK (modeled): $%.4f from advertised schedule rate %.3f exp %.1f "
             "(%.4f/sh)", modeled_fee, mk["fee_rate"], mk["fee_exponent"],
             modeled_fee / filled if filled else 0.0)
    if measured_fee is not None:
        verdict = ("MATCHES model — fees.assume_taker_rate: null is correct"
                   if abs(measured_fee - modeled_fee) < 0.005 else
                   "DIFFERS from model — recalibrate fees.assume_taker_rate")
        log.info("FEE CHECK (GROUND TRUTH): collateral $%.4f -> $%.4f = spent $%.4f; "
                 "less fill cost $%.4f => REAL FEE $%+.4f (%+.4f/sh). %s",
                 bal_before, bal_after, spent, cost, measured_fee,
                 measured_fee / filled if filled else 0.0, verdict)
    else:
        log.warning("FEE CHECK (GROUND TRUTH): balance read unavailable "
                    "(before=%s after=%s) — cannot measure the real fee.",
                    bal_before, bal_after)
    log.info("done. Position of ~%.2f %s shares is open; it settles at window close "
             "(or merge/redeem via the bot's on-chain loop).", filled, side.upper())


if __name__ == "__main__":
    main()
