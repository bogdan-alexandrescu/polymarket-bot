#!/usr/bin/env python3
"""
Copy Trader - Background daemon that copies trades from followed Polymarket users.

Polls activity every 60 seconds, detects new trades, and replicates them
with configurable sizing (fixed dollar amount or percentage of original).

Run via web UI or directly: python copy_trader.py
"""

import asyncio
import aiohttp
import os
import signal
import sys
import time
from datetime import datetime
from polymarket_client import PolymarketClient
from copy_trading_config import get_ct_manager, CopyTraderConfig
from log_manager import get_logger
from db import execute, init_tables


class CopyTrader:
    def __init__(self, check_interval: int = 60):
        self.client = PolymarketClient()
        self.config_manager = get_ct_manager()
        self.check_interval = check_interval
        self.running = True
        self.ct_log = get_logger('copy_trading')
        self.copied_trade_ids: set[str] = set()
        self.run_timestamp = time.time()

    def _reset_trades_for_run(self):
        """Reset run timestamp at the start of each cycle."""
        self.run_timestamp = time.time()

    def save_detected_trade(self, config: CopyTraderConfig, trade: dict):
        """Save a detected trade to the DB."""
        try:
            execute(
                """INSERT INTO detected_trades
                   (run_timestamp, handle, profile_name, side, title, outcome,
                    token_id, price, usdc_size, size, fill_count, timestamp)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (self.run_timestamp,
                 config.handle,
                 config.profile_name,
                 trade.get('side', '').upper(),
                 trade.get('title', 'Unknown'),
                 trade.get('outcome', ''),
                 trade.get('asset', ''),
                 float(trade.get('price', 0)),
                 trade.get('usdcSize', 0),
                 trade.get('size', 0),
                 trade.get('fill_count', 1),
                 time.time()),
            )
        except Exception:
            pass

    def save_executed_trade(self, config: CopyTraderConfig, trade: dict,
                            copy_amount: float, order_result: dict):
        """Save an executed copy trade to the DB."""
        price = 0
        shares = 0
        try:
            price = self.client.get_price(trade.get('asset', ''), trade.get('side', 'buy').lower())
            if price and price > 0:
                shares = copy_amount / price
        except Exception:
            pass
        try:
            execute(
                """INSERT INTO executed_trades
                   (run_timestamp, handle, profile_name, side, title, outcome,
                    token_id, price, usdc_size, size, order_id, timestamp)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (self.run_timestamp,
                 config.handle,
                 config.profile_name,
                 trade.get('side', '').upper() if isinstance(trade.get('side'), str) else trade.get('side', ''),
                 trade.get('title', 'Unknown'),
                 trade.get('outcome', ''),
                 trade.get('asset', ''),
                 price,
                 copy_amount,
                 shares,
                 order_result.get('orderID', ''),
                 time.time()),
            )
        except Exception:
            pass

    def log(self, msg: str):
        self.ct_log.info(msg)

    async def fetch_activity(self, wallet: str, start_timestamp: float) -> list[dict]:
        try:
            start_ts = int(start_timestamp)
            async with aiohttp.ClientSession() as session:
                params = {
                    "user": wallet,
                    "type": "TRADE",
                    "limit": 50,
                    "start": start_ts,
                }
                async with session.get(
                    "https://data-api.polymarket.com/activity",
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=30)
                ) as resp:
                    if resp.status != 200:
                        self.log(f"  [FAIL] Activity API returned {resp.status}")
                        return []
                    activities = await resp.json()
                    return activities if activities else []
        except Exception as e:
            self.log(f"  [FAIL] Error fetching activity: {e}")
            return []

    def calculate_copy_size(self, config: CopyTraderConfig, original_usdc_size: float) -> float:
        if original_usdc_size <= config.max_amount:
            return original_usdc_size
        return config.max_amount + (original_usdc_size * config.extra_pct)

    async def copy_buy_trade(self, config: CopyTraderConfig, activity: dict) -> bool:
        token_id = activity.get('asset', '')
        usdc_size = float(activity.get('usdcSize', 0))
        price = float(activity.get('price', 0))
        side = activity.get('outcome', 'Yes')

        copy_amount = self.calculate_copy_size(config, usdc_size)

        if copy_amount < 1:
            self.log(f"  [SKIP] BUY {side} — copy amount too small (${copy_amount:.2f})")
            return False

        self.log(f"  [COPY] BUY ${copy_amount:.2f} on {side} (original ${usdc_size:.2f} @ {price*100:.1f}c)")

        try:
            result = self.client.place_market_order(
                token_id=token_id,
                side="buy",
                amount=copy_amount,
            )

            if result.get("success") or result.get("orderID"):
                order_id = result.get('orderID', 'OK')
                self.log(f"  [DONE] BUY order placed — {order_id[:20]}")
                self.save_executed_trade(config, activity, copy_amount, result)
                return True
            else:
                self.log(f"  [FAIL] BUY rejected — {result.get('error', 'Unknown')}")
                return False
        except Exception as e:
            self.log(f"  [FAIL] BUY error — {e}")
            return False

    async def copy_sell_trade(self, config: CopyTraderConfig, activity: dict) -> bool:
        token_id = activity.get('asset', '')
        size = float(activity.get('size', 0))

        try:
            positions = await self.client.get_positions()
            my_pos = next((p for p in (positions or []) if p.get('asset') == token_id), None)

            if not my_pos or float(my_pos.get('size', 0)) <= 0:
                self.log(f"  [SKIP] SELL — not holding this position")
                return False

            my_size = float(my_pos.get('size', 0))

            self.log(f"  [COPY] SELL {my_size:.2f} shares (trader sold {size:.2f})")

            book = self.client.get_order_book(token_id)
            bids = book.bids if hasattr(book, 'bids') else book.get("bids", [])
            if not bids:
                self.log(f"  [FAIL] SELL — no bids available")
                return False

            best_bid = float(bids[0].price if hasattr(bids[0], 'price') else bids[0]['price'])
            sell_price = max(best_bid - 0.001, 0.01)

            result = self.client.place_order(
                token_id=token_id,
                side="sell",
                size=my_size,
                price=sell_price,
            )

            if result.get("success") or result.get("orderID"):
                order_id = result.get('orderID', 'OK')
                self.log(f"  [DONE] SELL order placed — {order_id[:20]}")
                sell_activity = {**activity, 'side': 'SELL'}
                self.save_executed_trade(config, sell_activity, my_size * sell_price, result)
                return True
            else:
                self.log(f"  [FAIL] SELL rejected — {result.get('error', 'Unknown')}")
                return False
        except Exception as e:
            self.log(f"  [FAIL] SELL error — {e}")
            return False

    async def process_config(self, config: CopyTraderConfig):
        self.log(f"@{config.handle} ({config.profile_name})")

        if config.last_check_timestamp is None:
            config.last_check_timestamp = time.time()
            self.config_manager.update(config.id, last_check_timestamp=config.last_check_timestamp)
            self.log(f"  First run — tracking new trades from now")
            return

        activities = await self.fetch_activity(config.wallet_address, config.last_check_timestamp)

        if not activities:
            self.log(f"  No new activity")
            config.last_check_timestamp = time.time()
            self.config_manager.update(config.id, last_check_timestamp=config.last_check_timestamp)
            return

        trades = [a for a in activities if a.get('type') == 'TRADE']

        if not trades:
            self.log(f"  No new trades")
            config.last_check_timestamp = time.time()
            self.config_manager.update(config.id, last_check_timestamp=config.last_check_timestamp)
            return

        new_trades = []
        for trade in trades:
            trade_id = f"{trade.get('transactionHash', '')}_{trade.get('asset', '')}_{trade.get('timestamp', '')}"
            if trade_id not in self.copied_trade_ids:
                new_trades.append(trade)
                self.copied_trade_ids.add(trade_id)

        if not new_trades:
            self.log(f"  No new trades (already processed)")
            config.last_check_timestamp = time.time()
            self.config_manager.update(config.id, last_check_timestamp=config.last_check_timestamp)
            return

        consolidated = {}
        for trade in new_trades:
            asset = trade.get('asset', '')
            side = trade.get('side', '').upper()
            key = (asset, side)

            if key not in consolidated:
                consolidated[key] = {
                    'asset': asset,
                    'side': side,
                    'title': trade.get('title', 'Unknown'),
                    'outcome': trade.get('outcome', ''),
                    'usdcSize': 0.0,
                    'size': 0.0,
                    'price': float(trade.get('price', 0)),
                    'fill_count': 0,
                }
            consolidated[key]['usdcSize'] += float(trade.get('usdcSize', 0))
            consolidated[key]['size'] += float(trade.get('size', 0))
            consolidated[key]['fill_count'] += 1

        self.log(f"  [DETECT] {len(new_trades)} fills -> {len(consolidated)} trades")

        for trade in consolidated.values():
            self.save_detected_trade(config, trade)

        copied = 0
        skipped = 0
        failed = 0

        for (asset, side), trade in consolidated.items():
            title = trade['title'][:50]
            outcome = trade['outcome']
            usdc = trade['usdcSize']

            self.log(f"  [TRADE] {side} {outcome} on '{title}' (${usdc:.2f})")

            try:
                if side == 'BUY':
                    success = await self.copy_buy_trade(config, trade)
                elif side == 'SELL':
                    success = await self.copy_sell_trade(config, trade)
                else:
                    self.log(f"  [SKIP] Unknown side '{side}'")
                    success = False

                if success:
                    copied += 1
                else:
                    skipped += 1
            except Exception as e:
                self.log(f"  [FAIL] Error — {e}")
                failed += 1

        config.last_check_timestamp = time.time()
        self.config_manager.update(config.id, last_check_timestamp=config.last_check_timestamp)

        parts = []
        if copied: parts.append(f"{copied} copied")
        if skipped: parts.append(f"{skipped} skipped")
        if failed: parts.append(f"{failed} failed")
        self.log(f"  [RESULT] {', '.join(parts) if parts else 'nothing to copy'}")

    async def run(self):
        self.run_count = 0

        configs = self.config_manager.list_enabled()
        self.log("[START] Copy Trader daemon started")
        self.log(f"[START] Following {len(configs)} trader(s), interval {self.check_interval}s")
        for config in configs:
            self.log(f"[START]   @{config.handle}: max ${config.max_amount:.0f} +{config.extra_pct*100:.0f}%")

        while self.running:
            configs = self.config_manager.list_enabled()

            if not configs:
                self.log("[WAIT] No enabled configs, waiting...")
                await asyncio.sleep(self.check_interval)
                continue

            self._reset_trades_for_run()

            self.run_count += 1
            run_start = time.time()
            self.log(f"[RUN] Cycle #{self.run_count} — checking {len(configs)} trader(s)")

            for config in configs:
                try:
                    await self.process_config(config)
                except Exception as e:
                    self.log(f"  [FAIL] @{config.handle} — {e}")

            elapsed = time.time() - run_start
            self.log(f"[END] Cycle #{self.run_count} complete ({elapsed:.1f}s) — next in {self.check_interval}s")

            await asyncio.sleep(self.check_interval)

        self.log("[STOP] Copy Trader daemon stopped")

    def stop(self):
        self.running = False


def handle_signal(signum, frame):
    print("\nReceived shutdown signal, stopping...")
    sys.exit(0)


async def main(check_interval: int = 60):
    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    init_tables()

    manager = get_ct_manager()
    manager.set_pid(os.getpid())

    try:
        trader = CopyTrader(check_interval=check_interval)
        await trader.run()
    finally:
        manager.clear_pid()


if __name__ == "__main__":
    init_tables()
    manager = get_ct_manager()
    configs = manager.list_enabled()

    if not configs:
        print("No enabled copy trading configs. Add traders via the web UI.")
        sys.exit(1)

    asyncio.run(main())
