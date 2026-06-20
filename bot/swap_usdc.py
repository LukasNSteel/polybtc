"""Swap native USDC -> USDC.e on Polygon (Uniswap v3) so it can be wrapped to pUSD.

CEX withdrawals deliver NATIVE USDC (0x3c49...3359), but Polymarket's
CollateralOnramp only wraps USDC.e (bridged, 0x2791...8174). This swaps native
USDC -> USDC.e through the Uniswap v3 0.01% pool (native<->bridged trades ~1:1).

Prereqs: EOA holds native USDC + a little POL. DRY RUN by default; --fire sends
the approve + swap txs.

    python -m bot.swap_usdc                  # quote only, no tx
    python -m bot.swap_usdc --fire           # swap ALL native USDC -> USDC.e
    python -m bot.swap_usdc --fire --usdc 5  # swap $5
    python -m bot.swap_usdc --fire --slippage 0.5   # max slippage %

Then: python -m bot.wrap_pusd --fire
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
log = logging.getLogger("swap_usdc")

NATIVE = "0x3c499c542cEF5E3811e1192cE70d8cC03d5c3359"   # native Polygon USDC (Circle)
USDCE = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"    # bridged USDC.e
ROUTER = "0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45"   # Uniswap v3 SwapRouter02
QUOTER = "0x61fFE014bA17989E743c5F6cB21bF9697530B21e"   # Uniswap v3 QuoterV2
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
QUOTER_ABI = [{"name": "quoteExactInputSingle", "type": "function", "stateMutability": "nonpayable",
    "inputs": [{"name": "params", "type": "tuple", "components": [
        {"name": "tokenIn", "type": "address"}, {"name": "tokenOut", "type": "address"},
        {"name": "amountIn", "type": "uint256"}, {"name": "fee", "type": "uint24"},
        {"name": "sqrtPriceLimitX96", "type": "uint160"}]}],
    "outputs": [{"name": "amountOut", "type": "uint256"}, {"name": "a", "type": "uint160"},
                {"name": "b", "type": "uint32"}, {"name": "c", "type": "uint256"}]}]
ROUTER_ABI = [{"name": "exactInputSingle", "type": "function", "stateMutability": "payable",
    "inputs": [{"name": "params", "type": "tuple", "components": [
        {"name": "tokenIn", "type": "address"}, {"name": "tokenOut", "type": "address"},
        {"name": "fee", "type": "uint24"}, {"name": "recipient", "type": "address"},
        {"name": "amountIn", "type": "uint256"}, {"name": "amountOutMinimum", "type": "uint256"},
        {"name": "sqrtPriceLimitX96", "type": "uint160"}]}],
    "outputs": [{"name": "amountOut", "type": "uint256"}]}]


def main() -> None:
    ap = argparse.ArgumentParser(description="swap native USDC -> USDC.e on Polygon")
    ap.add_argument("--fire", action="store_true", help="send approve + swap (default: quote only)")
    ap.add_argument("--usdc", type=float, default=0.0, help="native USDC to swap (0 = whole balance)")
    ap.add_argument("--fee", type=int, default=100, help="Uniswap v3 fee tier (default 100 = 0.01%%)")
    ap.add_argument("--slippage", type=float, default=0.5, help="max slippage %% (default 0.5)")
    ap.add_argument("--rpc", default=os.environ.get("POLYGON_RPC", "https://polygon-bor-rpc.publicnode.com"))
    args = ap.parse_args()

    from web3 import Web3

    key = os.environ.get("POLYMARKET_PRIVATE_KEY")
    if not key:
        raise SystemExit("set POLYMARKET_PRIVATE_KEY in .env first")

    w3 = Web3(Web3.HTTPProvider(args.rpc, request_kwargs={"timeout": 20}))
    from web3.middleware import ExtraDataToPOAMiddleware  # Polygon is POA
    w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
    acct = w3.eth.account.from_key(key)
    eoa = acct.address
    native = w3.eth.contract(address=Web3.to_checksum_address(NATIVE), abi=ERC20_ABI)
    usdce = w3.eth.contract(address=Web3.to_checksum_address(USDCE), abi=ERC20_ABI)
    quoter = w3.eth.contract(address=Web3.to_checksum_address(QUOTER), abi=QUOTER_ABI)
    router = w3.eth.contract(address=Web3.to_checksum_address(ROUTER), abi=ROUTER_ABI)

    pol = w3.eth.get_balance(eoa) / 1e18
    nat_bal = native.functions.balanceOf(eoa).call()
    log.info("EOA %s on chain %s", eoa, w3.eth.chain_id)
    log.info("  POL          : %.6f", pol)
    log.info("  USDC (native): %.6f", nat_bal / DECIMALS)
    log.info("  USDC.e       : %.6f", usdce.functions.balanceOf(eoa).call() / DECIMALS)

    amount = int(round(args.usdc * DECIMALS)) if args.usdc > 0 else nat_bal
    if amount <= 0:
        raise SystemExit("no native USDC to swap")
    if amount > nat_bal:
        raise SystemExit(f"requested {amount / DECIMALS:.2f} but native USDC balance is {nat_bal / DECIMALS:.2f}")

    quoted = quoter.functions.quoteExactInputSingle(
        (Web3.to_checksum_address(NATIVE), Web3.to_checksum_address(USDCE), amount, args.fee, 0)).call()[0]
    min_out = int(quoted * (1 - args.slippage / 100))
    log.info("quote: %.6f native USDC -> %.6f USDC.e (fee tier %d); min out %.6f at %.2f%% slippage",
             amount / DECIMALS, quoted / DECIMALS, args.fee, min_out / DECIMALS, args.slippage)

    if not args.fire:
        log.info("DRY RUN — no tx sent. Re-run with --fire to approve + swap.")
        return
    if pol == 0:
        raise SystemExit("EOA has 0 POL; cannot pay gas.")

    def send(fn, value=0):
        tx = fn.build_transaction({"from": eoa, "value": value,
                                   "nonce": w3.eth.get_transaction_count(eoa)})
        signed = acct.sign_transaction(tx)
        h = w3.eth.send_raw_transaction(signed.raw_transaction)
        rcpt = w3.eth.wait_for_transaction_receipt(h, timeout=180)
        if rcpt.status != 1:
            raise RuntimeError(f"tx reverted: {h.hex()}")
        return h.hex()

    if native.functions.allowance(eoa, Web3.to_checksum_address(ROUTER)).call() < amount:
        log.info("approving SwapRouter to spend native USDC...")
        log.info("  approve tx: %s", send(native.functions.approve(Web3.to_checksum_address(ROUTER), amount)))

    log.info("swapping...")
    params = (Web3.to_checksum_address(NATIVE), Web3.to_checksum_address(USDCE),
              args.fee, eoa, amount, min_out, 0)
    log.info("  swap tx: %s", send(router.functions.exactInputSingle(params)))

    log.info("after: native USDC %.6f | USDC.e %.6f",
             native.functions.balanceOf(eoa).call() / DECIMALS,
             usdce.functions.balanceOf(eoa).call() / DECIMALS)
    log.info("done — now run `python -m bot.wrap_pusd --fire` to wrap USDC.e -> pUSD.")


if __name__ == "__main__":
    main()
