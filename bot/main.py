"""Entry point.

Paper mode (default):  python -m bot.main
Live mode:             python -m bot.main --live
"""

import argparse
import asyncio
import logging
import os
import time

import truststore

truststore.inject_into_ssl()  # use the OS trust store (matches curl/browser behavior)

from .binance_feed import BinanceFeed
from .coinbase_feed import CoinbaseFeed
from .config import Config, live_credentials
from .execution import LiveExecutor, PaperExecutor, Portfolio
from .markets import MarketManager
from .orderbook import OrderBookFeed
from .strategy import Strategy


def setup_logging(log_dir: str) -> None:
    os.makedirs(log_dir, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(name)-9s %(levelname)-7s %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root.addHandler(sh)
    fh = logging.FileHandler(os.path.join(log_dir, f"session_{int(time.time())}.log"))
    fh.setFormatter(fmt)
    root.addHandler(fh)


async def amain(args: argparse.Namespace) -> None:
    cfg = Config.load(args.config)
    setup_logging(cfg.get("log_dir", default="logs"))
    log = logging.getLogger("main")

    perp_enabled = cfg.get("perp_lead", "enabled", default=True)
    coinbase_enabled = cfg.get("coinbase", "enabled", default=True)
    coinbase = (CoinbaseFeed(cfg.get("coinbase", "symbol", default="BTC-USD"))
                if coinbase_enabled else None)
    binance = BinanceFeed(
        cfg["binance_symbol"],
        vol_halflife_fast_sec=cfg.get("fair_value", "vol_halflife_fast_sec", default=60),
        vol_halflife_slow_sec=cfg.get("fair_value", "vol_halflife_slow_sec", default=600),
        min_vol_per_sec=cfg.get("fair_value", "min_vol_per_sec", default=2e-5),
        perp_symbol=(cfg.get("perp_lead", "symbol", default=cfg["binance_symbol"])
                     if perp_enabled else None),
        basis_halflife_sec=cfg.get("perp_lead", "basis_halflife_sec", default=120),
        coinbase=coinbase,
        coinbase_weight=cfg.get("coinbase", "weight", default=0.5),
    )
    liq_enabled = cfg.get("liquidations", "enabled", default=True)
    if liq_enabled:
        binance.configure_liquidations(
            min_notional_usd=cfg.get("liquidations", "min_notional_usd", default=50_000),
        )
    markets = MarketManager(
        enable_5m=cfg.get("markets", "five_minute", default=True),
        enable_15m=cfg.get("markets", "fifteen_minute", default=True),
        enable_hourly=cfg.get("markets", "hourly", default=True),
        enable_4h=cfg.get("markets", "four_hour", default=True),
    )
    feed = OrderBookFeed()
    state_file = cfg.get("state_file", default="state.json")
    portfolio = Portfolio(
        starting_cash=cfg.get("paper", "starting_cash", default=1000),
        state_file=None if args.fresh else state_file,
    )
    if not args.fresh:
        markets.expired.extend(portfolio.restore())

    tasks = []
    if args.live:
        creds = live_credentials()
        sig_type = cfg.get("live", "signature_type", default=1)
        onchain = None
        if sig_type == 0:
            from .onchain import OnChain
            onchain = OnChain(
                rpc_url=cfg.get("live", "polygon_rpc", default="https://polygon-rpc.com"),
                private_key=creds["private_key"],
            )
        else:
            log.warning("signature_type=%s: positions live in a proxy wallet; "
                        "on-chain merge/redeem disabled (use the Polymarket UI). "
                        "Use signature_type=0 (EOA) for automatic merging.", sig_type)
        executor = LiveExecutor(
            portfolio,
            host=cfg.get("live", "host", default="https://clob.polymarket.com"),
            chain_id=cfg.get("live", "chain_id", default=137),
            private_key=creds["private_key"],
            funder=creds["funder"],
            signature_type=sig_type,
            onchain=onchain,
            fak_min_fill_rate=cfg.get("fak_monitor", "min_fill_rate", default=0.50),
            fak_min_attempts=cfg.get("fak_monitor", "min_attempts", default=10),
            fak_window_size=cfg.get("fak_monitor", "window_size", default=30),
        )
        tasks.append(executor.run_user_feed())
        tasks.append(executor.process_onchain())
        log.warning("LIVE MODE: real orders will be placed.")
    else:
        executor = PaperExecutor(
            portfolio, feed,
            taker_latency_ms=cfg.get("paper", "taker_latency_ms", default=350),
            cancel_latency_ms=cfg.get("paper", "cancel_latency_ms", default=150),
            fak_min_fill_rate=cfg.get("fak_monitor", "min_fill_rate", default=0.50),
            fak_min_attempts=cfg.get("fak_monitor", "min_attempts", default=10),
            fak_window_size=cfg.get("fak_monitor", "window_size", default=30),
        )
        log.info("paper mode: simulated fills against live books "
                 "(taker latency %.0fms, cancel latency %.0fms)",
                 executor.taker_latency * 1000, executor.cancel_latency * 1000)

    strategy = Strategy(cfg, binance, markets, feed, executor, portfolio,
                        paper=not args.live)
    if liq_enabled:
        binance.on_liquidation.append(strategy.on_liquidation)
    tasks += [binance.run(), markets.run(), feed.run(), strategy.run()]
    if coinbase:
        tasks.append(coinbase.run())
    if perp_enabled or liq_enabled:
        tasks.append(binance.run_fstream())
    try:
        await asyncio.gather(*tasks)
    finally:
        portfolio.save()
        portfolio.log_summary()


def main() -> None:
    p = argparse.ArgumentParser(description="Polymarket BTC up/down market-making bot")
    p.add_argument("--live", action="store_true", help="place real orders (default: paper trade)")
    p.add_argument("--fresh", action="store_true", help="ignore saved state, start clean")
    p.add_argument("--config", default="config.yaml")
    args = p.parse_args()
    try:
        asyncio.run(amain(args))
    except KeyboardInterrupt:
        print("\nstopped.")


if __name__ == "__main__":
    main()
