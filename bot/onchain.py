"""On-chain merge/redeem of outcome token pairs on Polygon.

Polymarket routes pUSD-native CTF actions through thin collateral adapter
contracts: approve the adapter once, then call its mergePositions /
redeemPositions. For every merged Up+Down pair you receive $1.00 pUSD.

NOTE: this works when your positions are held directly by your EOA
(signature_type=0). Email/magic and browser-wallet accounts hold positions in
a proxy contract — merge those via the Polymarket UI instead.

Contract addresses: https://docs.polymarket.com/resources/contracts
"""

import asyncio
import logging

log = logging.getLogger("onchain")

CTF = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"            # ConditionalTokens
ADAPTER = "0xAdA100Db00Ca00073811820692005400218FcE1f"        # CtfCollateralAdapter
PUSD = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"           # pUSD collateral
ZERO32 = b"\x00" * 32
PARTITION = [1, 2]  # both outcomes of a binary market
USDC_DECIMALS = 10 ** 6

ADAPTER_ABI = [
    {"name": "mergePositions", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "", "type": "address"}, {"name": "", "type": "bytes32"},
                {"name": "_conditionId", "type": "bytes32"},
                {"name": "", "type": "uint256[]"}, {"name": "_amount", "type": "uint256"}],
     "outputs": []},
    {"name": "redeemPositions", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "", "type": "address"}, {"name": "", "type": "bytes32"},
                {"name": "_conditionId", "type": "bytes32"},
                {"name": "", "type": "uint256[]"}],
     "outputs": []},
]

CTF_ABI = [
    {"name": "balanceOf", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "owner", "type": "address"}, {"name": "id", "type": "uint256"}],
     "outputs": [{"name": "", "type": "uint256"}]},
    {"name": "isApprovedForAll", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "owner", "type": "address"}, {"name": "operator", "type": "address"}],
     "outputs": [{"name": "", "type": "bool"}]},
    {"name": "setApprovalForAll", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "operator", "type": "address"}, {"name": "approved", "type": "bool"}],
     "outputs": []},
    {"name": "payoutDenominator", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "", "type": "bytes32"}],
     "outputs": [{"name": "", "type": "uint256"}]},
]


class OnChain:
    def __init__(self, rpc_url: str, private_key: str):
        from web3 import Web3

        self.w3 = Web3(Web3.HTTPProvider(rpc_url))
        self.account = self.w3.eth.account.from_key(private_key)
        self.ctf = self.w3.eth.contract(address=Web3.to_checksum_address(CTF), abi=CTF_ABI)
        self.adapter = self.w3.eth.contract(address=Web3.to_checksum_address(ADAPTER), abi=ADAPTER_ABI)
        log.info("onchain ready: %s on chain %s", self.account.address, self.w3.eth.chain_id)

    # ---- sync internals (run via to_thread) ----

    def _send(self, fn) -> str:
        tx = fn.build_transaction({
            "from": self.account.address,
            "nonce": self.w3.eth.get_transaction_count(self.account.address),
        })
        signed = self.account.sign_transaction(tx)
        h = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = self.w3.eth.wait_for_transaction_receipt(h, timeout=120)
        if receipt.status != 1:
            raise RuntimeError(f"tx reverted: {h.hex()}")
        return h.hex()

    def _ensure_approval(self) -> None:
        if not self.ctf.functions.isApprovedForAll(self.account.address, self.adapter.address).call():
            log.info("approving CtfCollateralAdapter on ConditionalTokens (one-time)")
            self._send(self.ctf.functions.setApprovalForAll(self.adapter.address, True))

    def _pair_balance(self, token_up: str, token_down: str) -> int:
        bu = self.ctf.functions.balanceOf(self.account.address, int(token_up)).call()
        bd = self.ctf.functions.balanceOf(self.account.address, int(token_down)).call()
        return min(bu, bd)

    def _merge(self, condition_id: str, token_up: str, token_down: str) -> float:
        self._ensure_approval()
        amount = self._pair_balance(token_up, token_down)
        if amount < USDC_DECIMALS:  # less than $1 of pairs: not worth the gas
            return 0.0
        h = self._send(self.adapter.functions.mergePositions(
            PUSD, ZERO32, bytes.fromhex(condition_id.removeprefix("0x")), PARTITION, amount))
        usd = amount / USDC_DECIMALS
        log.info("MERGED %.2f pairs -> $%.2f pUSD (tx %s)", usd, usd, h)
        return usd

    def _redeem(self, condition_id: str) -> bool:
        cid = bytes.fromhex(condition_id.removeprefix("0x"))
        if self.ctf.functions.payoutDenominator(cid).call() == 0:
            return False  # oracle hasn't resolved yet; retry later
        self._ensure_approval()
        h = self._send(self.adapter.functions.redeemPositions(PUSD, ZERO32, cid, PARTITION))
        log.info("REDEEMED condition %s (tx %s)", condition_id[:10], h)
        return True

    # ---- async API ----

    async def merge(self, condition_id: str, token_up: str, token_down: str) -> float:
        return await asyncio.to_thread(self._merge, condition_id, token_up, token_down)

    async def redeem(self, condition_id: str) -> bool:
        return await asyncio.to_thread(self._redeem, condition_id)
