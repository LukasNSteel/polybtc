"""One-time live setup for a signature_type 0 (EOA) wallet.

Approves the Polymarket CLOB exchange contracts to spend the EOA's pUSD
collateral so the backend admits BUY orders. Run ONCE after funding the EOA
with pUSD collateral plus a little POL for gas:

    python -m bot.setup_eoa            # dry run: show what needs approving
    python -m bot.setup_eoa --fire     # send the on-chain approvals

For a plain EOA the CLOB's relayer endpoint (update_balance_allowance) is a
no-op — the EOA must set the ERC-20 allowance ON-CHAIN itself. We read the
spender contracts the CLOB expects from the balance-allowance endpoint and
approve pUSD for each. Idempotent: spenders already approved are skipped.
"""

import argparse
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
PUSD = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"   # pUSD collateral token
MAX_UINT = (1 << 256) - 1

ERC20_ABI = [
    {"name": "allowance", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "o", "type": "address"}, {"name": "s", "type": "address"}],
     "outputs": [{"name": "", "type": "uint256"}]},
    {"name": "approve", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "s", "type": "address"}, {"name": "a", "type": "uint256"}],
     "outputs": [{"name": "", "type": "bool"}]},
]


def main() -> None:
    ap = argparse.ArgumentParser(description="approve CLOB exchange to spend EOA pUSD")
    ap.add_argument("--fire", action="store_true", help="send approvals (default: dry run)")
    ap.add_argument("--rpc", default=os.environ.get("POLYGON_RPC",
                                                    "https://polygon-bor-rpc.publicnode.com"))
    args = ap.parse_args()

    from py_clob_client_v2 import AssetType, BalanceAllowanceParams, ClobClient
    from web3 import Web3
    from web3.middleware import ExtraDataToPOAMiddleware

    key = os.environ.get("POLYMARKET_PRIVATE_KEY")
    if not key:
        raise SystemExit("set POLYMARKET_PRIVATE_KEY in .env first")

    client = ClobClient(HOST, key=key, chain_id=137, signature_type=0)
    client.set_api_creds(client.create_or_derive_api_key())
    eoa = Web3.to_checksum_address(client.get_address())
    log.info("EOA address: %s", eoa)

    params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=0)
    ba = client.get_balance_allowance(params)
    log.info("collateral balance: %s pUSD", int(ba.get("balance", 0)) / 1e6)
    spenders = list(ba.get("allowances", {}).keys())
    if not spenders:
        raise SystemExit("CLOB returned no spender contracts — cannot determine what to approve")

    w3 = Web3(Web3.HTTPProvider(args.rpc, request_kwargs={"timeout": 20}))
    w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)  # Polygon is POA
    acct = w3.eth.account.from_key(key)
    pusd = w3.eth.contract(address=Web3.to_checksum_address(PUSD), abi=ERC20_ABI)
    pol = w3.eth.get_balance(eoa) / 1e18
    log.info("POL: %.6f", pol)

    need = []
    for sp in spenders:
        spender = Web3.to_checksum_address(sp)
        cur = pusd.functions.allowance(eoa, spender).call()
        status = "OK" if cur > 0 else "NEEDS APPROVAL"
        log.info("  spender %s : allowance %s [%s]", spender, cur, status)
        if cur == 0:
            need.append(spender)

    if not need:
        log.info("all spenders already approved — EOA is ready to trade.")
        return
    if not args.fire:
        log.info("DRY RUN — %d spender(s) need approval. Re-run with --fire to send.", len(need))
        return
    if pol == 0:
        raise SystemExit("EOA has 0 POL; cannot pay gas for approvals.")

    def send(fn):
        tx = fn.build_transaction({"from": eoa, "nonce": w3.eth.get_transaction_count(eoa)})
        signed = acct.sign_transaction(tx)
        h = w3.eth.send_raw_transaction(signed.raw_transaction)
        rcpt = w3.eth.wait_for_transaction_receipt(h, timeout=180)
        if rcpt.status != 1:
            raise RuntimeError(f"tx reverted: {h.hex()}")
        return h.hex()

    for spender in need:
        log.info("approving pUSD for %s ...", spender)
        log.info("  tx: %s", send(pusd.functions.approve(spender, MAX_UINT)))

    log.info("re-checking via CLOB...")
    log.info("after: %s", client.get_balance_allowance(params))
    log.info("done — EOA can now place BUY orders.")


if __name__ == "__main__":
    main()
