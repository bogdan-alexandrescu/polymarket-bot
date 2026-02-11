#!/usr/bin/env python3
"""
Profit Monitor - Monitors positions and auto-executes TP/SL orders.

Uses config file for persistent TP/SL settings.
Run via CLI: python main.py monitor start
"""

import asyncio
import os
import signal
import sys
from datetime import datetime
from polymarket_client import PolymarketClient
from monitor_config import get_manager, PositionConfig, LOG_FILE, PID_FILE


class ProfitMonitor:
    def __init__(self, check_interval: int = 60):
        self.client = PolymarketClient()
        self.config_manager = get_manager()
        self.check_interval = check_interval
        self.running = True
        self.sold_tokens = set()

    def log(self, msg: str):
        """Log message to file and stdout."""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{timestamp}] {msg}"
        print(line)

        # Also write to log file
        try:
            with open(LOG_FILE, 'a') as f:
                f.write(line + "\n")
        except:
            pass

    def get_full_order_book(self, token_id: str) -> dict:
        """Get the full order book with bids sorted highest-first, asks lowest-first."""
        try:
            book = self.client.get_order_book(token_id)
            # Sort bids by price descending (best bid first)
            bids = sorted(
                [(float(o.price), float(o.size)) for o in (book.bids or [])],
                key=lambda x: x[0],
                reverse=True
            )
            # Sort asks by price ascending (best ask first)
            asks = sorted(
                [(float(o.price), float(o.size)) for o in (book.asks or [])],
                key=lambda x: x[0]
            )
            return {"asks": asks, "bids": bids}
        except Exception as e:
            self.log(f"Error getting order book: {e}")
            return {"asks": [], "bids": []}

    def find_bids_at_price(self, token_id: str, min_price: float) -> dict:
        """Find bids at or above a minimum price."""
        book = self.get_full_order_book(token_id)
        bids = book["bids"]
        bids_sorted = sorted(bids, key=lambda x: x[0], reverse=True)

        matching_bids = [(price, size) for price, size in bids_sorted if price >= min_price]

        total_size = sum(size for _, size in matching_bids)
        total_value = sum(price * size for price, size in matching_bids)
        avg_price = total_value / total_size if total_size > 0 else 0

        return {
            "found": len(matching_bids) > 0,
            "count": len(matching_bids),
            "total_size": total_size,
            "total_value": total_value,
            "avg_price": avg_price,
            "best_bid": bids_sorted[0][0] if bids_sorted else 0,
        }

    def find_asks_at_price(self, token_id: str, max_price: float) -> dict:
        """Find asks at or below a maximum price (for stop loss via selling)."""
        book = self.get_full_order_book(token_id)
        asks = book["asks"]
        asks_sorted = sorted(asks, key=lambda x: x[0])  # Lowest first

        # For stop loss, we look at bids (what we can sell at)
        bids = book["bids"]
        bids_sorted = sorted(bids, key=lambda x: x[0], reverse=True)

        # Find bids at or below stop loss (we need to sell at market)
        best_bid = bids_sorted[0][0] if bids_sorted else 0

        return {
            "best_bid": best_bid,
            "triggered": best_bid <= max_price if best_bid > 0 else False,
            "best_ask": asks_sorted[0][0] if asks_sorted else 1.0,
        }

    async def get_actual_position_size(self, token_id: str) -> float:
        """Get actual position size from API."""
        try:
            positions = await self.client.get_positions()
            for p in positions:
                if p.get('asset') == token_id:
                    return float(p.get('size', 0))
            return 0
        except Exception as e:
            self.log(f"Error getting position size: {e}")
            return 0

    def execute_sell(self, token_id: str, shares: float, price: float) -> dict:
        """Execute a sell order."""
        try:
            result = self.client.place_order(
                token_id=token_id,
                side="sell",
                size=shares,
                price=price,
            )
            return result
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def check_and_redeem(self):
        """Check all positions for redeemable ones and auto-redeem."""
        import time as _time
        try:
            positions = await self.client.get_positions()
            if not positions:
                return

            # Only redeem positions with actual value (winning side)
            redeemable = [
                p for p in positions
                if p.get('redeemable')
                and float(p.get('size', 0)) > 0
                and float(p.get('currentValue', 0)) > 0.01
            ]

            if not redeemable:
                return

            # Check if EOA has gas (POL/MATIC) before attempting on-chain txs
            try:
                w3 = self.client._get_w3()
                from eth_account import Account
                eoa = Account.from_key(self.client.private_key).address
                gas_balance = w3.eth.get_balance(eoa)
                gas_pol = w3.from_wei(gas_balance, 'ether')
                if gas_balance == 0:
                    total_value = sum(float(p.get('currentValue', 0)) for p in redeemable)
                    self.log(f"Found {len(redeemable)} redeemable position(s) (~${total_value:.2f}), "
                             f"but EOA {eoa[:10]}... has 0 POL for gas. Fund it to auto-redeem.")
                    return
                self.log(f"EOA gas balance: {gas_pol:.4f} POL")
            except Exception as e:
                self.log(f"Cannot check gas balance ({e}), skipping redemption")
                return

            total_value = sum(float(p.get('currentValue', 0)) for p in redeemable)
            self.log(f"Found {len(redeemable)} redeemable position(s) (~${total_value:.2f})")
            self.log("-" * 70)

            # Process up to 5 per cycle to avoid rate limits
            redeemed_count = 0
            for pos in redeemable[:5]:
                title = pos.get('title', 'Unknown')[:50]
                outcome = pos.get('outcome', '?')
                size = float(pos.get('size', 0))
                value = float(pos.get('currentValue', 0))
                condition_id = pos.get('conditionId', '')
                neg_risk = pos.get('negativeRisk', False)

                self.log(f"  Redeeming: {outcome} on '{title}' ({size:.2f} shares, ~${value:.2f})")

                if not condition_id:
                    self.log(f"    No conditionId, skipping")
                    continue

                try:
                    result = self.client.redeem_position(condition_id, negative_risk=neg_risk)

                    if result.get('success'):
                        tx_hash = result.get('tx_hash', '')
                        self.log(f"    REDEEMED! Tx: {tx_hash[:20]}...")
                        self.log(f"    Winnings: ~${value:.2f} USDC")
                        redeemed_count += 1
                    else:
                        error = result.get('error', 'Unknown error')
                        self.log(f"    Redemption failed: {error}")
                except Exception as e:
                    self.log(f"    Redemption error: {e}")

                # Delay between redemptions to avoid RPC rate limits
                _time.sleep(3)

            if redeemed_count > 0:
                self.log(f"  Redeemed {redeemed_count}/{len(redeemable)} positions this cycle")
            if len(redeemable) > 5:
                self.log(f"  {len(redeemable) - 5} more will be attempted next cycle")
            self.log("")
        except Exception as e:
            self.log(f"Error checking redeemable positions: {e}")

    async def check_position(self, config: PositionConfig) -> dict:
        """Check a single position for TP/SL triggers."""
        result = {
            "config_id": config.id,
            "name": config.name,
            "action": None,
            "details": {},
        }

        if config.token_id in self.sold_tokens:
            result["action"] = "already_sold"
            return result

        tp_target = config.get_tp_target()
        sl_target = config.get_sl_target()

        book = self.get_full_order_book(config.token_id)
        best_bid = book["bids"][0][0] if book["bids"] else 0
        best_ask = book["asks"][0][0] if book["asks"] else 1.0

        # Calculate midpoint and spread for better price estimation
        midpoint = (best_bid + best_ask) / 2 if best_bid > 0 else best_ask
        spread = best_ask - best_bid if best_bid > 0 else 1.0

        result["details"] = {
            "best_bid": best_bid,
            "best_ask": best_ask,
            "midpoint": midpoint,
            "spread": spread,
            "tp_target": tp_target,
            "sl_target": sl_target,
        }

        # Check Take Profit - trigger when best bid >= TP target
        if tp_target:
            bids_info = self.find_bids_at_price(config.token_id, tp_target)
            # Trigger TP if best bid is at or above TP target price
            if bids_info["best_bid"] >= tp_target:
                result["action"] = "take_profit"
                result["details"]["tp_info"] = bids_info
                return result

        # Check Stop Loss - use midpoint price and require reasonable spread
        # This prevents false triggers on thin/illiquid order books
        if sl_target and midpoint > 0:
            # Only trigger SL if:
            # 1. Spread is reasonable (< 50%) - indicates real market activity
            # 2. Midpoint price is at or below SL target
            if spread < 0.50 and midpoint <= sl_target:
                result["action"] = "stop_loss"
                result["details"]["sl_triggered_at"] = best_bid
                return result
            elif spread >= 0.50:
                # Market is too thin/illiquid - don't trigger SL
                result["details"]["sl_skipped_reason"] = f"spread too wide ({spread*100:.1f}%)"

        result["action"] = "hold"
        return result

    async def run(self, configs: list[PositionConfig]):
        """Main monitoring loop."""
        self.log("=" * 70)
        self.log("PROFIT MONITOR STARTED")
        self.log("=" * 70)

        # Cleanup: remove configs for positions that no longer exist
        positions = await self.client.get_positions()
        position_tokens = {p.get('asset') for p in positions} if positions else set()

        valid_configs = []
        for config in configs:
            if config.token_id in position_tokens:
                valid_configs.append(config)
            else:
                self.log(f"  ⚠️ Removing orphan config: {config.name} (position no longer exists)")
                self.config_manager.delete(config.id)

        configs = valid_configs
        self.log(f"Monitoring {len(configs)} positions")
        self.log(f"Check interval: {self.check_interval}s")
        self.log("")

        for config in configs:
            tp = config.get_tp_target()
            sl = config.get_sl_target()
            tp_str = f"TP: {tp*100:.1f}%" if tp else "TP: -"
            sl_str = f"SL: {sl*100:.1f}%" if sl else "SL: -"
            self.log(f"  {config.name}: Entry {config.entry_price*100:.1f}% | {tp_str} | {sl_str}")

        self.log("=" * 70)
        self.log("")

        active_configs = {c.id: c for c in configs}

        while self.running and active_configs:
            # Check for redeemable positions first
            await self.check_and_redeem()

            self.log(f"Scanning {len(active_configs)} positions...")
            self.log("-" * 70)

            for config_id, config in list(active_configs.items()):
                check_result = await self.check_position(config)
                action = check_result["action"]
                details = check_result["details"]

                tp_target = config.get_tp_target()
                sl_target = config.get_sl_target()
                tp_str = f"TP: {tp_target*100:.1f}%" if tp_target else "TP: -"
                sl_str = f"SL: {sl_target*100:.1f}%" if sl_target else "SL: -"

                self.log(f"  {config.name} ({config.side})")
                self.log(f"    Entry: {config.entry_price*100:.1f}% | {tp_str} | {sl_str}")
                midpoint = details.get('midpoint', 0)
                spread = details.get('spread', 0)
                self.log(f"    Market: Mid {midpoint*100:.1f}% | Spread {spread*100:.1f}%")

                if action == "take_profit":
                    tp_info = details.get("tp_info", {})
                    self.log(f"    🎯 TAKE PROFIT TRIGGERED!")
                    self.log(f"       Best bid: {tp_info.get('best_bid', 0)*100:.1f}% >= TP: {tp_target*100:.1f}%")
                    self.log(f"       Executing sell: {config.shares:.2f} shares @ {tp_target*100:.1f}%...")

                    result = self.execute_sell(config.token_id, config.shares, tp_target)
                    if result.get("success") or result.get("orderID"):
                        self.log(f"       ✅ SOLD! Order: {result.get('orderID', 'OK')[:20]}...")
                        self.sold_tokens.add(config.token_id)
                        self.config_manager.delete(config_id)  # Remove from persistent storage
                        del active_configs[config_id]
                    else:
                        error_msg = result.get('error', 'Unknown')
                        self.log(f"       ❌ Sell failed: {error_msg}")

                        # If balance/allowance error, try with actual position size
                        if 'balance' in str(error_msg).lower() or 'allowance' in str(error_msg).lower():
                            self.log(f"       🔄 Checking actual position size...")
                            actual_size = await self.get_actual_position_size(config.token_id)

                            if actual_size <= 0:
                                self.log(f"       ⚠️ Position no longer exists, removing from monitor")
                                self.sold_tokens.add(config.token_id)
                                self.config_manager.delete(config_id)  # Remove from persistent storage
                                del active_configs[config_id]
                            elif abs(actual_size - config.shares) > 0.01:
                                self.log(f"       🔄 Retrying with actual size: {actual_size:.2f} shares...")
                                result = self.execute_sell(config.token_id, actual_size, tp_target)
                                if result.get("success") or result.get("orderID"):
                                    self.log(f"       ✅ SOLD! Order: {result.get('orderID', 'OK')[:20]}...")
                                    self.sold_tokens.add(config.token_id)
                                    self.config_manager.delete(config_id)  # Remove from persistent storage
                                    del active_configs[config_id]
                                else:
                                    self.log(f"       ❌ Retry failed: {result.get('error', 'Unknown')}")

                elif action == "stop_loss":
                    sl_price = details.get("sl_triggered_at", 0)
                    self.log(f"    🛑 STOP LOSS TRIGGERED!")
                    self.log(f"       Best bid ({sl_price*100:.1f}%) <= SL target ({sl_target*100:.1f}%)")

                    # Ensure sell price stays within valid bounds (0.01 to 0.99)
                    sell_price = max(sl_price - 0.001, 0.01)
                    self.log(f"       Executing sell: {config.shares:.2f} shares @ {sell_price*100:.1f}%...")

                    result = self.execute_sell(config.token_id, config.shares, sell_price)
                    if result.get("success") or result.get("orderID"):
                        self.log(f"       ✅ SOLD! Order: {result.get('orderID', 'OK')[:20]}...")
                        self.sold_tokens.add(config.token_id)
                        self.config_manager.delete(config_id)  # Remove from persistent storage
                        del active_configs[config_id]
                    else:
                        error_msg = result.get('error', 'Unknown')
                        self.log(f"       ❌ Sell failed: {error_msg}")

                        # If balance/allowance error, try with actual position size
                        if 'balance' in str(error_msg).lower() or 'allowance' in str(error_msg).lower():
                            self.log(f"       🔄 Checking actual position size...")
                            actual_size = await self.get_actual_position_size(config.token_id)

                            if actual_size <= 0:
                                self.log(f"       ⚠️ Position no longer exists, removing from monitor")
                                self.sold_tokens.add(config.token_id)
                                self.config_manager.delete(config_id)  # Remove from persistent storage
                                del active_configs[config_id]
                            elif abs(actual_size - config.shares) > 0.01:
                                self.log(f"       🔄 Retrying with actual size: {actual_size:.2f} shares...")
                                result = self.execute_sell(config.token_id, actual_size, sell_price)
                                if result.get("success") or result.get("orderID"):
                                    self.log(f"       ✅ SOLD! Order: {result.get('orderID', 'OK')[:20]}...")
                                    self.sold_tokens.add(config.token_id)
                                    self.config_manager.delete(config_id)  # Remove from persistent storage
                                    del active_configs[config_id]
                                else:
                                    self.log(f"       ❌ Retry failed: {result.get('error', 'Unknown')}")

                elif action == "already_sold":
                    self.log(f"    ⏭️ Already sold, skipping")
                    self.config_manager.delete(config_id)  # Remove from persistent storage
                    del active_configs[config_id]

                else:
                    # Check if SL was skipped due to thin market
                    sl_skip_reason = details.get("sl_skipped_reason")
                    if sl_skip_reason:
                        self.log(f"    ⏳ Holding (SL check skipped: {sl_skip_reason})")
                    else:
                        self.log(f"    ⏳ Holding...")

                self.log("")

            if not active_configs:
                self.log("🎉 All positions closed!")
                break

            self.log(f"Active: {len(active_configs)} | Next check in {self.check_interval}s")
            self.log("=" * 70)
            self.log("")

            await asyncio.sleep(self.check_interval)

        self.log("Monitor stopped.")

    def stop(self):
        """Stop the monitor."""
        self.running = False


def handle_signal(signum, frame):
    """Handle shutdown signals."""
    print("\nReceived shutdown signal, stopping...")
    sys.exit(0)


async def main(configs: list[PositionConfig], check_interval: int = 60):
    """Main entry point."""
    # Set up signal handlers
    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    # Write PID file
    manager = get_manager()
    manager.set_monitor_pid(os.getpid())

    try:
        monitor = ProfitMonitor(check_interval=check_interval)
        await monitor.run(configs)
    finally:
        manager.clear_monitor_pid()


if __name__ == "__main__":
    # When run directly, load configs from file
    manager = get_manager()
    configs = manager.list_enabled()

    if not configs:
        print("No enabled configs found. Use 'python main.py monitor add' to add positions.")
        sys.exit(1)

    asyncio.run(main(configs))
