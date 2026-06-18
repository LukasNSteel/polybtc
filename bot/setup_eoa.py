"""One-time live setup for a signature_type 0 (EOA) wallet.

Approves the Polymarket CLOB exchange to spend the EOA's pUSD collateral so the
backend admits BUY orders. Run ONCE after funding the EOA with pUSD collateral
plus a little POL for gas:

    python -m bot.setup_eoa

Idempotent — re-running is a no-op if the allowance is already set. The
approval transaction is signed by and sent from the EOA (costs a small amount
of POL gas).
"""

import logging
import os

import truststore
from dotenv import load_dotenv

truststore.inject_into_ssl()
load_dotenv()

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("setup_eoa")

HOST = "https://clob.polymarket.com"


def main() -> None:
    from py_clob_client_v2 import AssetType, BalanceAllowanceParams, ClobClient

    key = os.environ.get("POLYMARKET_PRIVATE_KEY")
    if not key:
        raise SystemExit("set POLYMARKET_PRIVATE_KEY in .env first")

    client = ClobClient(HOST, key=key, chain_id=137, signature_type=0)
    client.set_api_creds(client.create_or_derive_api_key())
    log.info("EOA address: %s", client.get_address())

    params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=0)
    try:
        log.info("current collateral balance/allowance: %s",
                 client.get_balance_allowance(params))
    except Exception as e:  # noqa: BLE001 — informational only
        log.warning("could not read balance/allowance: %s", e)

    log.info("approving CLOB exchange to spend pUSD collateral "
             "(signs on-chain from the EOA; needs a little POL for gas)...")
    client.update_balance_allowance(params)

    try:
        log.info("after: %s", client.get_balance_allowance(params))
    except Exception as e:  # noqa: BLE001
        log.warning("could not re-read balance/allowance: %s", e)
    log.info("done — EOA can place BUY orders once it holds pUSD collateral.")


if __name__ == "__main__":
    main()
