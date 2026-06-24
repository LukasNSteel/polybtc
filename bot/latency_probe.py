"""Read-only latency probe: which CLOB server op owns the order-ack time?

Times, on a warm shared httpx connection, the cheap baseline (GET /time),
a pure order-book read, and the proxy-wallet balance/allowance refresh +
read — WITHOUT placing any order. If update_balance_allowance is the slow
one, the ~600-2000ms order-ack latency is the server verifying the proxy's
collateral per order, and keeping that server-side cache warm (not changing
wallets) is the lever. Run: .venv/bin/python -m bot.latency_probe
"""
import os
import time

from py_clob_client_v2 import (AssetType, BalanceAllowanceParams, ClobClient)


def _t(label, fn, n=4):
    times = []
    last = None
    for _ in range(n):
        t0 = time.monotonic()
        try:
            last = fn()
            ok = True
        except Exception as e:  # noqa: BLE001
            last = f"ERR {e}"
            ok = False
        times.append((time.monotonic() - t0) * 1000)
        if not ok:
            break
    ms = ", ".join(f"{x:.0f}" for x in times)
    print(f"{label:38} [{ms}] ms")
    return last


def main():
    key = os.environ["POLYMARKET_PRIVATE_KEY"]
    funder = os.environ.get("POLYMARKET_FUNDER") or None
    sig = int(os.environ.get("PROBE_SIG_TYPE", "3"))
    kwargs = {"key": key, "chain_id": 137, "signature_type": sig}
    if funder:
        kwargs["funder"] = funder
    c = ClobClient("https://clob.polymarket.com", **kwargs)
    c.set_api_creds(c.create_or_derive_api_key())
    print(f"signature_type={sig} funder={(funder or 'EOA')[:12]}…\n")

    p = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=sig)

    # warm the socket first so we measure server time, not TLS setup
    c.get_server_time()
    _t("GET /time (baseline RTT+server)", c.get_server_time)
    _t("update_balance_allowance (refresh cache)", lambda: c.update_balance_allowance(p))
    _t("get_balance_allowance (cached read)", lambda: c.get_balance_allowance(p))

    tok = os.environ.get("PROBE_TOKEN")
    if tok:
        _t("get_order_book (pure read)", lambda: c.get_order_book(tok))


if __name__ == "__main__":
    main()
