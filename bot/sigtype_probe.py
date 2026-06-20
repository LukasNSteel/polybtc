"""Probe which signature_type the V2 CLOB backend accepts for this wallet.

A Polymarket deposit/proxy wallet must be addressed with the signature_type that
matches how it was deployed, or the venue rejects the maker with
"maker address not allowed, please use the deposit wallet flow". This posts ONE
deep, non-marketable GTD BUY (well below the book, so it rests and never fills)
for each candidate type, reports which is ACCEPTED, then cancels. Put the winner
in config.yaml -> live.signature_type.

    python -m bot.sigtype_probe

Needs POLYMARKET_PRIVATE_KEY (+ POLYMARKET_FUNDER for proxy/deposit wallets) in
.env. No position is opened (orders are cancelled immediately).
"""

import logging
import os
import time

import truststore
from dotenv import load_dotenv

truststore.inject_into_ssl()
load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    datefmt="%H:%M:%S")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("sigtype")

HOST = "https://clob.polymarket.com"
# 3 = POLY_1271 deposit wallet (email/Magic), 2 = Gnosis Safe (browser wallet),
# 1 = Magic proxy. 0 (plain EOA) is excluded — the venue rejects it.
CANDIDATES = [3, 2, 1]


def main() -> None:
    from py_clob_client_v2 import (
        AssetType, BalanceAllowanceParams, ClobClient, OrderArgs, OrderType,
        PartialCreateOrderOptions, Side,
    )
    from py_clob_client_v2.exceptions import PolyApiException

    from .live_trade_smoke import _best_ask, _candidate_markets

    key = os.environ.get("POLYMARKET_PRIVATE_KEY")
    funder = os.environ.get("POLYMARKET_FUNDER") or None
    if not key:
        raise SystemExit("set POLYMARKET_PRIVATE_KEY in .env first")
    if not funder:
        log.warning("POLYMARKET_FUNDER is empty — proxy/deposit types (1/2/3) need it. "
                    "Set it to your Polymarket deposit address.")

    # one liquid market; rest a deep bid that cannot fill
    mk = None
    for cand in _candidate_markets():
        for token in (cand["token_up"], cand["token_down"]):
            ask = _best_ask(ClobClient(HOST, key=key, chain_id=137, signature_type=2,
                                       funder=funder) if funder else
                            ClobClient(HOST, key=key, chain_id=137), token)
            if ask:
                mk, probe_token = cand, token
                break
        if mk:
            break
    if not mk:
        raise SystemExit("no liquid market found to probe")

    tick = mk["tick"]
    price = max(tick, round(round(0.10 / tick) * tick, 6))  # deep, won't fill
    log.info("probe market: %s (tick=%s neg_risk=%s)", mk["title"], tick, mk["neg_risk"])
    log.info("probe order: BUY 5 @ %.3f on token ...%s (deep, non-marketable)",
             price, probe_token[-6:])

    results: dict[int, str] = {}
    for st in CANDIDATES:
        log.info("---- signature_type=%d ----", st)
        try:
            kwargs = {"key": key, "chain_id": 137, "signature_type": st}
            if funder:
                kwargs["funder"] = funder
            client = ClobClient(HOST, **kwargs)
            client.set_api_creds(client.create_or_derive_api_key())
        except Exception as e:  # noqa: BLE001
            results[st] = f"client/creds error: {e}"
            log.error("  creds: %s", e)
            continue
        # deposit wallets (type 3) need a balance sync before orders are admitted
        try:
            client.update_balance_allowance(
                BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=st))
        except Exception as e:  # noqa: BLE001
            log.warning("  balance sync: %s", e)
        try:
            signed = client.create_order(
                OrderArgs(price=price, size=5.0, side=Side.BUY, token_id=probe_token,
                          expiration=int(time.time()) + 150),
                PartialCreateOrderOptions(neg_risk=mk["neg_risk"]))
            resp = client.post_order(signed, OrderType.GTD)
            oid = (resp.get("orderID") or resp.get("orderId")) if isinstance(resp, dict) else None
            results[st] = f"ACCEPTED (orderID={str(oid)[:18]})" if oid else f"posted: {resp}"
            log.info("  %s", results[st])
        except PolyApiException as e:
            results[st] = f"REJECTED: {e}"
            log.error("  REJECTED: %s", e)
        except Exception as e:  # noqa: BLE001
            results[st] = f"error: {e}"
            log.error("  error: %s", e)
        finally:
            try:
                client.cancel_all()
            except Exception as e:  # noqa: BLE001
                log.warning("  cancel_all: %s", e)

    log.info("==== SUMMARY ====")
    for st in CANDIDATES:
        log.info("  signature_type=%d -> %s", st, results.get(st, "(skipped)"))
    winners = [st for st, r in results.items() if r.startswith("ACCEPTED")]
    if winners:
        log.info("USE signature_type=%d in config.yaml (live.signature_type)", winners[0])


if __name__ == "__main__":
    main()
