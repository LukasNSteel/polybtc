"""Wrap USDC.e -> pUSD on the EOA so it can post collateral to the CLOB.

pUSD is Polymarket's collateral token (ERC-20 on Polygon, 1:1 USDC-backed). The
website wraps USDC into pUSD automatically for its proxy wallets, but a plain
EOA (signature_type 0) has to wrap itself through the CollateralOnramp:

  1. approve the Onramp to spend the EOA's USDC.e
  2. call wrap(USDC.e, EOA, amount) -> mints pUSD to the EOA

Prereqs: the EOA holds USDC.e (bridged USDC, 0x2791...8174) plus a little POL
for gas. DRY RUN by default; pass --fire to send the approve + wrap txs.

    python -m bot.wrap_pusd                 # show balances, no tx
    python -m bot.wrap_pusd --fire          # wrap ALL USDC.e -> pUSD
    python -m bot.wrap_pusd --fire --usdc 5 # wrap $5 of USDC.e

Docs: https://docs.polymarket.com/concepts/pusd
"""

import argparse
import logging
import os

import truststore
from dotenv import load_dotenv

truststore.inject_into_ssl()
load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("wrap_pusd")

ONRAMP = "0x93070a847efEf7F70739046A929D47a521F5B8ee"   # CollateralOnramp (USDC.e -> pUSD)
USDCE = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"    # bridged USDC.e
PUSD = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"     # pUSD collateral
DECIMALS = 10 ** 6

ERC20_ABI = [
    {"name": "balanceOf", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "o", "type": "address"}], "outputs": [{"name": "", "type": "uint256"}]},
    {"name": "allowance", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "o", "type": "address"}, {"name": "s", "type": "address"}],
     "outputs": [{"name": "", "type": "uint256"}]},
    {"name": "approve", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "s", "type": "address"}, {"name": "a", "type": "uint256"}],
     "outputs": [{"name": "", "type": "bool"}]},
]
ONRAMP_ABI = [
    {"name": "wrap", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "_asset", "type": "address"}, {"name": "_to", "type": "address"},
                {"name": "_amount", "type": "uint256"}], "outputs": []},
]


def main() -> None:
    ap = argparse.ArgumentParser(description="wrap USDC.e -> pUSD on the EOA")
    ap.add_argument("--fire", action="store_true", help="send the approve + wrap txs (default: dry run)")
    ap.add_argument("--usdc", type=float, default=0.0, help="USDC.e to wrap (0 = wrap entire balance)")
    ap.add_argument("--rpc", default=os.environ.get("POLYGON_RPC", "https://polygon-bor-rpc.publicnode.com"))
    args = ap.parse_args()

    from web3 import Web3

    key = os.environ.get("POLYMARKET_PRIVATE_KEY")
    if not key:
        raise SystemExit("set POLYMARKET_PRIVATE_KEY in .env first")

    w3 = Web3(Web3.HTTPProvider(args.rpc, request_kwargs={"timeout": 20}))
    acct = w3.eth.account.from_key(key)
    eoa = acct.address
    usdce = w3.eth.contract(address=Web3.to_checksum_address(USDCE), abi=ERC20_ABI)
    pusd = w3.eth.contract(address=Web3.to_checksum_address(PUSD), abi=ERC20_ABI)
    onramp = w3.eth.contract(address=Web3.to_checksum_address(ONRAMP), abi=ONRAMP_ABI)

    pol = w3.eth.get_balance(eoa) / 1e18
    usdce_bal = usdce.functions.balanceOf(eoa).call()
    pusd_bal = pusd.functions.balanceOf(eoa).call()
    log.info("EOA %s on chain %s", eoa, w3.eth.chain_id)
    log.info("  POL   : %.6f", pol)
    log.info("  USDC.e: %.6f", usdce_bal / DECIMALS)
    log.info("  pUSD  : %.6f", pusd_bal / DECIMALS)

    amount = int(round(args.usdc * DECIMALS)) if args.usdc > 0 else usdce_bal
    if amount <= 0:
        raise SystemExit("no USDC.e to wrap — fund the EOA with USDC.e first")
    if amount > usdce_bal:
        raise SystemExit(f"requested {amount / DECIMALS:.2f} USDC.e but balance is "
                         f"{usdce_bal / DECIMALS:.2f}")
    log.info("plan: wrap %.6f USDC.e -> pUSD (recipient %s)", amount / DECIMALS, eoa)

    if not args.fire:
        if pol == 0:
            log.warning("EOA has 0 POL — fund a little POL before --fire (gas for 2 txs)")
        log.info("DRY RUN — no tx sent. Re-run with --fire to approve + wrap.")
        return

    if pol == 0:
        raise SystemExit("EOA has 0 POL; cannot pay gas. Fund POL first.")

    def send(fn):
        tx = fn.build_transaction({"from": eoa, "nonce": w3.eth.get_transaction_count(eoa)})
        signed = acct.sign_transaction(tx)
        h = w3.eth.send_raw_transaction(signed.raw_transaction)
        rcpt = w3.eth.wait_for_transaction_receipt(h, timeout=180)
        if rcpt.status != 1:
            raise RuntimeError(f"tx reverted: {h.hex()}")
        return h.hex()

    allowance = usdce.functions.allowance(eoa, Web3.to_checksum_address(ONRAMP)).call()
    if allowance < amount:
        log.info("approving CollateralOnramp to spend USDC.e...")
        log.info("  approve tx: %s", send(usdce.functions.approve(Web3.to_checksum_address(ONRAMP), amount)))

    log.info("wrapping...")
    log.info("  wrap tx: %s", send(onramp.functions.wrap(
        Web3.to_checksum_address(USDCE), eoa, amount)))

    log.info("after: USDC.e %.6f | pUSD %.6f",
             usdce.functions.balanceOf(eoa).call() / DECIMALS,
             pusd.functions.balanceOf(eoa).call() / DECIMALS)
    log.info("done — now run `python -m bot.setup_eoa` to approve the CLOB exchange.")


if __name__ == "__main__":
    main()
