#!/usr/bin/env python3
"""
Polymarket Trading Bot - Main Entry Point

Usage:
    python main.py search "bitcoin"          # Search markets
    python main.py price <token_id>          # Get current price
    python main.py book <token_id>           # Get order book
    python main.py buy <token_id> <amount>   # Buy tokens (market order)
    python main.py sell <token_id> <amount>  # Sell tokens (market order)
    python main.py monitor                   # Start monitoring (interactive)
    python main.py positions                 # View positions
    python main.py orders                    # View open orders
    python main.py cancel <order_id>         # Cancel order
    python main.py scan                      # Scan for profitable opportunities
    python main.py scan --hours 48 --top 10  # Scan with custom parameters
"""

import argparse
import asyncio
import json
import signal
import sys
from typing import Optional

from polymarket_client import PolymarketClient
from monitor import MarketMonitor, TriggerDirection
from sms_alerts import SMSAlerter
from opportunity_scanner import OpportunityScanner
from scanner_config import ScannerConfig
from monitor_config import get_manager, MonitorConfigManager
from db import execute, init_tables
import config
import os
import subprocess


def print_json(data, indent=2):
    """Pretty print JSON data."""
    print(json.dumps(data, indent=indent, default=str))


async def cmd_search(client: PolymarketClient, query: str):
    """Search for markets."""
    markets = await client.search_markets(query)
    if not markets:
        print("No markets found.")
        return

    for m in markets[:10]:
        print(f"\n{'='*60}")
        event_name = m.get("_event", "")
        if event_name:
            print(f"Event: {event_name}")
        print(f"Market: {m.get('question', 'N/A')}")
        print(f"Condition ID: {m.get('conditionId', 'N/A')}")

        # Try tokens array first, then clobTokenIds
        tokens = m.get("tokens", [])
        if tokens:
            for t in tokens:
                outcome = t.get("outcome", "?")
                token_id = t.get("tokenId", "N/A")
                price = t.get("price", 0)
                print(f"  {outcome}: {float(price)*100:.1f}% (Token: {token_id})")
        else:
            # Parse from clobTokenIds and outcomePrices
            clob_ids = m.get("clobTokenIds", "[]")
            prices = m.get("outcomePrices", "[]")
            outcomes = m.get("outcomes", '["Yes", "No"]')
            try:
                import json
                clob_ids = json.loads(clob_ids) if isinstance(clob_ids, str) else clob_ids
                prices = json.loads(prices) if isinstance(prices, str) else prices
                outcomes = json.loads(outcomes) if isinstance(outcomes, str) else outcomes
                for i, outcome in enumerate(outcomes):
                    token_id = clob_ids[i] if i < len(clob_ids) else "N/A"
                    price = float(prices[i]) if i < len(prices) else 0
                    print(f"  {outcome}: {price*100:.1f}% (Token: {token_id})")
            except:
                pass


def cmd_price(client: PolymarketClient, token_id: str):
    """Get token price."""
    mid = client.get_midpoint_price(token_id)
    spread = client.get_spread(token_id)
    print(f"Midpoint: {mid*100:.2f}%")
    print(f"Bid: {spread['bid']*100:.2f}%")
    print(f"Ask: {spread['ask']*100:.2f}%")
    print(f"Spread: {spread['spread']*100:.2f}%")


def cmd_book(client: PolymarketClient, token_id: str, depth: int = 5):
    """Show order book."""
    book = client.get_order_book(token_id)

    asks = book.asks if hasattr(book, 'asks') else book.get("asks", [])
    bids = book.bids if hasattr(book, 'bids') else book.get("bids", [])

    print("\n📕 ASKS (Sell Orders)")
    for order in reversed(asks[:depth]):
        price = float(order.price if hasattr(order, 'price') else order['price'])
        size = float(order.size if hasattr(order, 'size') else order['size'])
        print(f"  {price*100:6.2f}% | {size:>10.2f}")

    print("  " + "-" * 20)

    print("📗 BIDS (Buy Orders)")
    for order in bids[:depth]:
        price = float(order.price if hasattr(order, 'price') else order['price'])
        size = float(order.size if hasattr(order, 'size') else order['size'])
        print(f"  {price*100:6.2f}% | {size:>10.2f}")


def cmd_buy(
    client: PolymarketClient,
    token_id: str,
    amount: float,
    price: Optional[float] = None,
):
    """Place a buy order."""
    if price:
        result = client.buy_yes(token_id, amount, price)
    else:
        result = client.buy_yes(token_id, amount)
    print("Order placed:")
    print_json(result)


def cmd_sell(
    client: PolymarketClient,
    token_id: str,
    amount: float,
    price: Optional[float] = None,
):
    """Place a sell order."""
    if price:
        result = client.sell_yes(token_id, amount, price)
    else:
        result = client.sell_yes(token_id, amount)
    print("Order placed:")
    print_json(result)


async def cmd_positions(client: PolymarketClient):
    """Show positions."""
    positions = await client.get_positions()
    if not positions:
        print("No positions found.")
        return

    for p in positions:
        print(f"\n{'='*60}")
        print(f"Market: {p.get('title', 'N/A')}")
        print(f"Outcome: {p.get('outcome', 'N/A')}")
        print(f"Size: {p.get('size', 0)} shares")
        print(f"Avg Price: ${p.get('avgPrice', 0):.4f} ({p.get('avgPrice', 0)*100:.1f}%)")
        print(f"Current Price: ${p.get('curPrice', 0):.4f} ({p.get('curPrice', 0)*100:.1f}%)")
        print(f"Current Value: ${p.get('currentValue', 0):.2f}")
        pnl = p.get('cashPnl', 0)
        pnl_pct = p.get('percentPnl', 0)
        pnl_sign = '+' if pnl >= 0 else ''
        print(f"P&L: {pnl_sign}${pnl:.2f} ({pnl_sign}{pnl_pct:.1f}%)")
        print(f"Token ID: {p.get('asset', 'N/A')}")


def cmd_orders(client: PolymarketClient):
    """Show open orders."""
    orders = client.get_orders()
    if not orders:
        print("No open orders.")
        return
    print_json(orders)


def cmd_cancel(client: PolymarketClient, order_id: str):
    """Cancel an order."""
    if order_id.lower() == "all":
        result = client.cancel_all_orders()
    else:
        result = client.cancel_order(order_id)
    print("Cancelled:")
    print_json(result)


async def cmd_scan(client: PolymarketClient, args):
    """Scan for profitable opportunities."""
    # Configure scanner
    scanner_config = ScannerConfig(
        max_hours_to_expiry=args.hours,
        min_profit_pct=args.min_profit,
        risk_mode=args.risk,
        auto_execute=args.auto_execute,
        fixed_amount=args.amount,
    )

    scanner = OpportunityScanner(client, scanner_config)

    print("=" * 70)
    print("🔍 OPPORTUNITY SCANNER - Finding Profitable Bets")
    print("=" * 70)
    print(f"  Mode: {args.risk.upper()}")
    print(f"  Max hours to expiry: {args.hours}h")
    print(f"  Min profit target: {args.min_profit*100:.1f}%")
    if args.amount:
        print(f"  Amount per position: ${args.amount:.2f}")
    else:
        print(f"  Amount per position: 10% of cash balance")
    print(f"  Auto-execute: {'ON' if args.auto_execute else 'OFF'}")
    if args.tp:
        print(f"  Take Profit: {args.tp*100:.1f}%")
    if args.sl:
        print(f"  Stop Loss: {args.sl*100:.1f}%")
    print("=" * 70)
    print()

    # Run scan
    opportunities = await scanner.scan()

    if not opportunities:
        print("\n❌ No opportunities found matching criteria.")
        print("   Try adjusting parameters:")
        print("   --hours 48      (look further out)")
        print("   --min-profit 0.02  (lower profit threshold)")
        print("   --risk moderate (allow more risk)")
        return

    # Show top opportunities
    top_n = min(args.top, len(opportunities))
    print(f"\n🎯 TOP {top_n} OPPORTUNITIES (sorted by lowest risk):\n")

    for i, opp in enumerate(opportunities[:top_n], 1):
        risk_emoji = "🟢" if opp.risk_score < 0.3 else "🟡" if opp.risk_score < 0.5 else "🔴"
        print(f"{'='*70}")
        print(f"#{i} {risk_emoji} {opp.title}")
        print(f"{'='*70}")
        print(f"  Event: {opp.event_title}")
        print(f"  Expires: {opp.end_date.strftime('%Y-%m-%d %H:%M')} UTC ({opp.hours_to_expiry:.1f}h)")
        print()
        print(f"  📊 RECOMMENDATION: BUY {opp.recommended_side}")
        print(f"     Entry Price: {opp.entry_price*100:.1f}%")
        print(f"     Expected Resolution: {opp.expected_resolution*100:.0f}%")
        print(f"     Expected Profit: +{opp.expected_profit_pct*100:.1f}%")
        print()
        print(f"  📈 RISK ANALYSIS:")
        print(f"     Confidence: {opp.confidence_score*100:.0f}%")
        print(f"     Risk Score: {opp.risk_score*100:.0f}% (lower=better)")
        print(f"     Liquidity: ${opp.liquidity:,.0f}")
        print(f"     Spread: {opp.spread*100:.1f}%")
        print(f"     24h Volume: ${opp.volume_24h:,.0f}")
        print()
        if opp.news_summary:
            print(f"  📰 NEWS: {opp.news_summary[:100]}...")
        if opp.triggering_event_detected:
            print(f"  ⚠️  WARNING: Triggering event may have occurred!")
        print()
        print(f"  💰 SIZING:")
        print(f"     Recommended: ${opp.recommended_amount:.2f}")
        print(f"     Potential Profit: ${opp.potential_profit:.2f}")
        print()
        print(f"  Token ID: {opp.token_id}")
        print()

    # Execution
    if args.auto_execute:
        print("\n🤖 AUTO-EXECUTE MODE - Placing orders...")
        executed_positions = []

        for opp in opportunities[:top_n]:
            if opp.recommended_amount > 0:
                print(f"\n  Executing: {opp.title[:50]}...")
                result = await scanner.execute_opportunity(opp)
                if result.get("success") or result.get("orderID"):
                    print(f"  ✅ Order placed: {result.get('orderID', '')[:20]}...")
                    executed_positions.append(opp)
                else:
                    print(f"  ❌ Failed: {result.get('error', 'Unknown error')}")

        # Add to profit monitor if TP or SL specified
        if executed_positions and (args.tp or args.sl):
            print(f"\n📊 Adding {len(executed_positions)} positions to profit monitor...")
            manager = get_manager()

            for opp in executed_positions:
                try:
                    # Check if already exists
                    existing = manager.get_by_token(opp.token_id)
                    if existing:
                        # Update existing config
                        updates = {}
                        if args.tp:
                            updates['take_profit_pct'] = args.tp
                        if args.sl:
                            updates['stop_loss_pct'] = args.sl
                        manager.update(existing.id, **updates)
                        print(f"  📝 Updated [{existing.id}] {opp.title[:40]}...")
                    else:
                        # Add new config
                        config = manager.add(
                            token_id=opp.token_id,
                            name=opp.title[:50],
                            side=opp.recommended_side,
                            shares=opp.recommended_amount / opp.entry_price,
                            entry_price=opp.entry_price,
                            take_profit_pct=args.tp,
                            stop_loss_pct=args.sl,
                        )
                        tp_str = f"TP: {config.get_tp_target()*100:.1f}%" if config.get_tp_target() else ""
                        sl_str = f"SL: {config.get_sl_target()*100:.1f}%" if config.get_sl_target() else ""
                        print(f"  ✅ Added [{config.id}] {opp.title[:40]}... {tp_str} {sl_str}")
                except Exception as e:
                    print(f"  ⚠️ Could not add {opp.title[:30]}: {e}")

            # Restart monitor if running, otherwise suggest starting it
            if manager.is_monitor_running():
                restart_monitor_if_running(manager)
            else:
                print(f"\n💡 Start the profit monitor with: python main.py pm start")
    else:
        print("\n" + "=" * 70)
        print("💡 To execute a recommendation, run:")
        print(f"   python main.py buy <token_id> <amount> --price <entry_price>")
        print()
        print("   Or enable auto-execute with TP/SL:")
        print(f"   python main.py scan --auto-execute --tp 0.03 --sl 0.05")
        print("=" * 70)


async def cmd_pm_status(client: PolymarketClient, manager: MonitorConfigManager):
    """Show profit monitor status."""
    pid = manager.get_monitor_pid()

    if pid:
        print(f"✅ Profit Monitor is RUNNING (PID: {pid})")
    else:
        print("❌ Profit Monitor is NOT running")

    configs = manager.list_all()
    enabled = [c for c in configs if c.enabled]
    print(f"\nConfigurations: {len(configs)} total, {len(enabled)} enabled")

    if configs:
        print("\nConfigured positions:")
        for c in configs:
            status = "ON" if c.enabled else "OFF"
            tp = c.get_tp_target()
            sl = c.get_sl_target()
            tp_str = f"TP: {tp*100:.1f}%" if tp else "TP: -"
            sl_str = f"SL: {sl*100:.1f}%" if sl else "SL: -"

            # Get current price
            try:
                mid = client.get_midpoint_price(c.token_id)
                cur_str = f"Now: {mid*100:.1f}%"
            except:
                cur_str = "Now: ?"

            print(f"  [{c.id}] {c.name[:40]} ({c.side})")
            print(f"      Entry: {c.entry_price*100:.1f}% | {cur_str} | {tp_str} | {sl_str} | {status}")


async def cmd_pm_list(client: PolymarketClient, manager: MonitorConfigManager):
    """List all profit monitor configs."""
    configs = manager.list_all()

    if not configs:
        print("No configurations found.")
        print("Use 'python main.py pm add' to add a position.")
        return

    print(f"{'='*80}")
    print(f"PROFIT MONITOR CONFIGURATIONS ({len(configs)} total)")
    print(f"{'='*80}")

    for c in configs:
        status = "✅ ON" if c.enabled else "❌ OFF"
        tp = c.get_tp_target()
        sl = c.get_sl_target()

        # Calculate gain/loss percentages from prices
        tp_gain_pct = ((tp / c.entry_price) - 1) * 100 if tp else 0
        sl_loss_pct = (1 - (sl / c.entry_price)) * 100 if sl else 0

        # Get current price
        try:
            mid = client.get_midpoint_price(c.token_id)
            cur_pnl = ((mid / c.entry_price) - 1) * 100
            cur_str = f"Current: {mid*100:.1f}% ({cur_pnl:+.1f}%)"
        except:
            cur_str = "Current: ?"

        print(f"\n[{c.id}] {c.name}")
        print(f"  Side: {c.side} | Shares: {c.shares:.2f} | Entry: {c.entry_price*100:.1f}%")
        print(f"  {cur_str}")
        print(f"  Take Profit: {tp*100:.1f}% (+{tp_gain_pct:.1f}% gain)" if tp else "  Take Profit: Not set")
        print(f"  Stop Loss: {sl*100:.1f}% (-{sl_loss_pct:.1f}% loss)" if sl else "  Stop Loss: Not set")
        print(f"  Status: {status}")
        print(f"  Token: {c.token_id}")


async def cmd_pm_add(client: PolymarketClient, manager: MonitorConfigManager, args):
    """Add a new position to monitor."""
    # Get current positions to help user
    positions = await client.get_positions()

    if not positions:
        print("No positions found in your portfolio.")
        return

    print("Your current positions:")
    print("-" * 60)
    for i, p in enumerate(positions, 1):
        title = p.get('title', 'Unknown')[:50]
        outcome = p.get('outcome', '?')
        size = float(p.get('size', 0))
        avg_price = float(p.get('avgPrice', 0))
        token_id = p.get('asset', '')
        print(f"{i}. {title}")
        print(f"   {outcome}: {size:.2f} shares @ {avg_price*100:.1f}%")
        print(f"   Token: {token_id[:40]}...")
        print()

    # Check if position already exists
    if args.token_id:
        existing = manager.get_by_token(args.token_id)
        if existing:
            print(f"Config already exists for this token: {existing.id}")
            print("Use 'pm edit' to modify it.")
            return

        # Find position info
        pos = next((p for p in positions if p.get('asset') == args.token_id), None)
        if not pos:
            print("Token not found in your positions.")
            return

        name = pos.get('title', 'Unknown')[:50]
        side = pos.get('outcome', 'Unknown')
        shares = float(pos.get('size', 0))
        entry_price = float(pos.get('avgPrice', 0))

        config = manager.add(
            token_id=args.token_id,
            name=name,
            side=side,
            shares=shares,
            entry_price=entry_price,
            take_profit_pct=args.tp,
            stop_loss_pct=args.sl,
        )

        print(f"\n✅ Added configuration:")
        print(f"   ID: {config.id}")
        print(f"   {config.name} ({config.side})")
        print(f"   Entry: {config.entry_price*100:.1f}%")
        if config.get_tp_target():
            print(f"   Take Profit: {config.get_tp_target()*100:.1f}%")
        if config.get_sl_target():
            print(f"   Stop Loss: {config.get_sl_target()*100:.1f}%")

        # Restart monitor if running
        restart_monitor_if_running(manager)
    else:
        print("\nTo add a position, use:")
        print("  python main.py pm add --token <token_id> --tp 0.03 --sl 0.05")
        print("\nExample (3% take profit, 5% stop loss):")
        print(f"  python main.py pm add --token {positions[0].get('asset', 'TOKEN_ID')} --tp 0.03 --sl 0.05")


async def cmd_pm_add_all(client: PolymarketClient, manager: MonitorConfigManager, args):
    """Add all open positions to monitor with TP/SL."""
    if not args.tp and not args.sl:
        print("Error: Must specify at least --tp or --sl")
        print("Example: python main.py pm add-all --tp 0.03 --sl 0.05")
        return

    # Get current positions
    positions = await client.get_positions()

    if not positions:
        print("No positions found in your portfolio.")
        return

    print(f"Found {len(positions)} open positions")
    print("-" * 60)

    added = 0
    updated = 0
    skipped = 0

    for p in positions:
        token_id = p.get('asset', '')
        name = p.get('title', 'Unknown')[:50]
        side = p.get('outcome', 'Unknown')
        shares = float(p.get('size', 0))
        entry_price = float(p.get('avgPrice', 0))

        # Check if already exists
        existing = manager.get_by_token(token_id)

        if existing:
            if args.overwrite:
                # Update existing
                updates = {}
                if args.tp:
                    updates['take_profit_pct'] = args.tp
                if args.sl:
                    updates['stop_loss_pct'] = args.sl
                manager.update(existing.id, **updates)
                print(f"📝 Updated [{existing.id}] {name}")
                updated += 1
            else:
                print(f"⏭️  Skipped [{existing.id}] {name} (already exists)")
                skipped += 1
        else:
            # Add new
            config = manager.add(
                token_id=token_id,
                name=name,
                side=side,
                shares=shares,
                entry_price=entry_price,
                take_profit_pct=args.tp,
                stop_loss_pct=args.sl,
            )
            tp_str = f"TP: {config.get_tp_target()*100:.1f}%" if config.get_tp_target() else ""
            sl_str = f"SL: {config.get_sl_target()*100:.1f}%" if config.get_sl_target() else ""
            print(f"✅ Added [{config.id}] {name} | {tp_str} {sl_str}")
            added += 1

    print("-" * 60)
    print(f"Summary: {added} added, {updated} updated, {skipped} skipped")

    if skipped > 0 and not args.overwrite:
        print("\nTip: Use --overwrite to update existing configs")

    # Restart monitor if running, otherwise suggest starting it
    if added > 0 or updated > 0:
        if manager.is_monitor_running():
            restart_monitor_if_running(manager)
        else:
            print("\nStart the monitor with: python main.py pm start")


async def cmd_pm_edit(manager: MonitorConfigManager, args):
    """Edit an existing config."""
    config = manager.get(args.config_id)
    if not config:
        print(f"Config not found: {args.config_id}")
        return

    updates = {}
    if args.tp is not None:
        updates['take_profit_pct'] = args.tp if args.tp > 0 else None
    if args.sl is not None:
        updates['stop_loss_pct'] = args.sl if args.sl > 0 else None
    if args.enable:
        updates['enabled'] = True
    if args.disable:
        updates['enabled'] = False

    if not updates:
        print("No changes specified. Use --tp, --sl, --enable, or --disable")
        return

    config = manager.update(args.config_id, **updates)

    print(f"✅ Updated [{config.id}] {config.name}:")
    if config.get_tp_target():
        print(f"   Take Profit: {config.get_tp_target()*100:.1f}%")
    else:
        print(f"   Take Profit: Not set")
    if config.get_sl_target():
        print(f"   Stop Loss: {config.get_sl_target()*100:.1f}%")
    else:
        print(f"   Stop Loss: Not set")
    print(f"   Enabled: {'Yes' if config.enabled else 'No'}")

    # Restart monitor if running
    restart_monitor_if_running(manager)


async def cmd_pm_delete(manager: MonitorConfigManager, args):
    """Delete a config."""
    config = manager.get(args.config_id)
    if not config:
        print(f"Config not found: {args.config_id}")
        return

    print(f"Deleting: [{config.id}] {config.name}")
    manager.delete(args.config_id)
    print("✅ Deleted")

    # Restart monitor if running
    restart_monitor_if_running(manager)


async def cmd_pm_delete_all(manager: MonitorConfigManager, args):
    """Delete all configs."""
    configs = manager.list_all()

    if not configs:
        print("No configurations to delete.")
        return

    # Confirm unless -y flag
    if not args.yes:
        print(f"This will delete {len(configs)} configuration(s):")
        for c in configs:
            print(f"  [{c.id}] {c.name}")
        response = input("\nAre you sure? [y/N]: ").strip().lower()
        if response not in ('y', 'yes'):
            print("Cancelled.")
            return

    # Stop monitor first if running
    was_running = manager.is_monitor_running()
    if was_running:
        stop_monitor_sync(manager, silent=True)

    # Delete all
    count = 0
    for c in configs:
        manager.delete(c.id)
        count += 1

    print(f"✅ Deleted {count} configuration(s)")

    if was_running:
        print("Monitor stopped (no configs remaining)")


async def cmd_pm_sell_all(client: PolymarketClient, manager: MonitorConfigManager, args):
    """Sell all positions at current market price."""
    # Get actual positions from API
    positions = await client.get_positions()

    if not positions:
        print("No positions to sell.")
        return

    print(f"Found {len(positions)} position(s) to sell:")
    print("-" * 60)

    for p in positions:
        title = p.get('title', 'Unknown')[:50]
        outcome = p.get('outcome', '?')
        size = float(p.get('size', 0))
        cur_price = float(p.get('curPrice', 0))
        value = float(p.get('currentValue', 0))
        print(f"  {title}")
        print(f"    {outcome}: {size:.2f} shares @ {cur_price*100:.1f}% = ${value:.2f}")

    # Confirm unless -y flag
    if not args.yes:
        response = input("\nSell all positions at market price? [y/N]: ").strip().lower()
        if response not in ('y', 'yes'):
            print("Cancelled.")
            return

    print("\nExecuting sells...")
    print("-" * 60)

    sold = 0
    failed = 0

    for p in positions:
        token_id = p.get('asset', '')
        title = p.get('title', 'Unknown')[:40]
        outcome = p.get('outcome', '?')
        size = float(p.get('size', 0))

        print(f"\n  Selling: {title} ({outcome})")

        # Get current best bid
        try:
            book = client.get_order_book(token_id)
            bids = book.bids if hasattr(book, 'bids') else book.get("bids", [])
            if not bids:
                print(f"    ❌ No bids available")
                failed += 1
                continue

            best_bid = float(bids[0].price if hasattr(bids[0], 'price') else bids[0]['price'])
            # Sell slightly below best bid to ensure fill
            sell_price = max(best_bid - 0.001, 0.01)

            print(f"    Best bid: {best_bid*100:.1f}%, selling {size:.2f} shares @ {sell_price*100:.1f}%...")

            result = client.place_order(
                token_id=token_id,
                side="sell",
                size=size,
                price=sell_price,
            )

            if result.get("success") or result.get("orderID"):
                print(f"    ✅ Order placed: {result.get('orderID', 'OK')[:20]}...")
                sold += 1

                # Remove from PM config if exists
                config = manager.get_by_token(token_id)
                if config:
                    manager.delete(config.id)
                    print(f"    Removed from PM config")
            else:
                print(f"    ❌ Failed: {result.get('error', 'Unknown')}")
                failed += 1

        except Exception as e:
            print(f"    ❌ Error: {e}")
            failed += 1

    print("\n" + "-" * 60)
    print(f"Summary: {sold} sold, {failed} failed")

    # Stop monitor if all positions sold
    if sold > 0 and manager.is_monitor_running():
        remaining = manager.list_enabled()
        if not remaining:
            stop_monitor_sync(manager, silent=True)
            print("Monitor stopped (no positions remaining)")


async def cmd_pm_start(client: PolymarketClient, manager: MonitorConfigManager, args):
    """Start the profit monitor."""
    # Check if already running
    if manager.is_monitor_running():
        pid = manager.get_monitor_pid()
        print(f"Monitor is already running (PID: {pid})")
        print("Use 'pm stop' to stop it first.")
        return

    # Get enabled configs
    configs = manager.list_enabled()

    if not configs:
        print("No enabled configurations found.")
        print("Use 'pm add' to add positions or 'pm edit --enable' to enable existing ones.")
        return

    # Check if positions still exist
    positions = await client.get_positions()
    position_tokens = {p.get('asset') for p in positions}

    valid_configs = []
    invalid_configs = []

    for c in configs:
        if c.token_id in position_tokens:
            valid_configs.append(c)
        else:
            invalid_configs.append(c)

    if invalid_configs:
        print("⚠️  Some configured positions no longer exist:")
        for c in invalid_configs:
            print(f"   [{c.id}] {c.name} - Position not found")
        print()

    if not valid_configs:
        print("No valid positions to monitor.")
        return

    # Confirm each position
    if not args.yes:
        print(f"\nPositions to monitor ({len(valid_configs)}):")
        print("-" * 60)

        confirmed = []
        for c in valid_configs:
            tp = c.get_tp_target()
            sl = c.get_sl_target()
            tp_str = f"TP: {tp*100:.1f}%" if tp else "TP: -"
            sl_str = f"SL: {sl*100:.1f}%" if sl else "SL: -"

            print(f"\n[{c.id}] {c.name} ({c.side})")
            print(f"  Entry: {c.entry_price*100:.1f}% | {tp_str} | {sl_str}")

            response = input("  Monitor this position? [Y/n]: ").strip().lower()
            if response in ('', 'y', 'yes'):
                confirmed.append(c)
            else:
                print("  Skipped.")

        valid_configs = confirmed

        if not valid_configs:
            print("\nNo positions selected. Aborting.")
            return

    print(f"\n🚀 Starting profit monitor with {len(valid_configs)} positions...")

    # Save confirmed configs to a temp file for the subprocess
    import json
    import tempfile

    config_data = {c.id: {
        'id': c.id,
        'token_id': c.token_id,
        'name': c.name,
        'side': c.side,
        'shares': c.shares,
        'entry_price': c.entry_price,
        'take_profit_pct': c.take_profit_pct,
        'take_profit_price': c.take_profit_price,
        'stop_loss_pct': c.stop_loss_pct,
        'stop_loss_price': c.stop_loss_price,
        'enabled': True,
    } for c in valid_configs}

    # Start the monitor process
    import sys
    monitor_script = os.path.join(os.path.dirname(__file__), 'profit_monitor.py')

    # Use nohup to detach
    cmd = f"nohup python -u {monitor_script} > /dev/null 2>&1 &"

    subprocess.Popen(cmd, shell=True, start_new_session=True)

    import time
    time.sleep(2)

    if manager.is_monitor_running():
        pid = manager.get_monitor_pid()
        print(f"✅ Monitor started (PID: {pid})")
        print(f"   Use 'pm status' to check status")
        print(f"   Use 'pm log' to view logs")
        print(f"   Use 'pm stop' to stop")
    else:
        print("❌ Failed to start monitor. Use 'pm log' to check logs.")


def stop_monitor_sync(manager: MonitorConfigManager, silent: bool = False) -> bool:
    """Stop the profit monitor (sync version). Returns True if it was running."""
    pid = manager.get_monitor_pid()

    if not pid:
        return False

    if not silent:
        print(f"Stopping monitor (PID: {pid})...")

    try:
        os.kill(pid, 15)  # SIGTERM
        import time
        time.sleep(1)

        # Check if stopped
        try:
            os.kill(pid, 0)
            # Still running, force kill
            os.kill(pid, 9)
        except ProcessLookupError:
            pass

        manager.clear_monitor_pid()
        if not silent:
            print("✅ Monitor stopped")
        return True
    except Exception as e:
        if not silent:
            print(f"Error stopping monitor: {e}")
        manager.clear_monitor_pid()
        return True


def start_monitor_sync(manager: MonitorConfigManager, silent: bool = False) -> bool:
    """Start the profit monitor (sync version). Returns True on success."""
    configs = manager.list_enabled()

    if not configs:
        if not silent:
            print("No enabled configurations to monitor.")
        return False

    monitor_script = os.path.join(os.path.dirname(__file__), 'profit_monitor.py')
    cmd = f"nohup python -u {monitor_script} > /dev/null 2>&1 &"

    subprocess.Popen(cmd, shell=True, start_new_session=True)

    import time
    time.sleep(2)

    if manager.is_monitor_running():
        pid = manager.get_monitor_pid()
        if not silent:
            print(f"✅ Monitor started (PID: {pid})")
        return True
    else:
        if not silent:
            print("❌ Failed to start monitor")
        return False


def restart_monitor_if_running(manager: MonitorConfigManager):
    """Restart the monitor if it was running."""
    if manager.is_monitor_running():
        print("\n🔄 Restarting profit monitor with updated config...")
        stop_monitor_sync(manager, silent=True)
        import time
        time.sleep(1)
        if start_monitor_sync(manager, silent=True):
            pid = manager.get_monitor_pid()
            print(f"✅ Monitor restarted (PID: {pid})")
        else:
            print("❌ Failed to restart monitor")


async def cmd_pm_stop(manager: MonitorConfigManager):
    """Stop the profit monitor."""
    pid = manager.get_monitor_pid()

    if not pid:
        print("Monitor is not running.")
        return

    stop_monitor_sync(manager)


async def cmd_pm_log(manager: MonitorConfigManager, args):
    """Show monitor logs."""
    lines = args.lines if hasattr(args, 'lines') else 50

    rows = execute(
        """SELECT time, message FROM daemon_logs
           WHERE channel = 'profit_monitor'
           ORDER BY id DESC LIMIT %s""",
        (lines,), fetch=True,
    )
    rows.reverse()
    if not rows:
        print("No logs found.")
        return
    for r in rows:
        print(f"[{r['time']}] {r['message']}")


async def cmd_monitor_interactive(client: PolymarketClient, alerter: SMSAlerter):
    """Interactive monitoring mode."""
    monitor = MarketMonitor(client, alerter, poll_interval=5.0)

    print("=" * 60)
    print("POLYMARKET MONITOR - Interactive Mode")
    print("=" * 60)
    print("\nCommands:")
    print("  add <condition_id>              - Add market to monitor")
    print("  alert <condition_id> <YES|NO> <threshold>  - Add price alert")
    print("  auto <condition_id> <YES|NO> <price> <above|below> <buy|sell> <amount>")
    print("  start                           - Start monitoring")
    print("  stop                            - Stop monitoring")
    print("  status                          - Show status")
    print("  quit                            - Exit")
    print()

    loop_task = None

    def handle_sigint(sig, frame):
        monitor.stop()
        print("\nExiting...")
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_sigint)

    while True:
        try:
            cmd = input("monitor> ").strip()
            if not cmd:
                continue

            parts = cmd.split()
            action = parts[0].lower()

            if action == "quit" or action == "q":
                monitor.stop()
                break

            elif action == "add" and len(parts) >= 2:
                cond_id = parts[1]
                market_data = await client.get_market(cond_id)
                tokens = market_data.get("tokens", [])
                yes_token = next((t["tokenId"] for t in tokens if t["outcome"] == "Yes"), None)
                no_token = next((t["tokenId"] for t in tokens if t["outcome"] == "No"), None)
                name = market_data.get("question", cond_id)[:50]

                monitor.add_market(cond_id, name, yes_token, no_token)
                print(f"Added: {name}")

            elif action == "alert" and len(parts) >= 4:
                cond_id = parts[1]
                outcome = parts[2].upper()
                threshold = float(parts[3])
                monitor.add_price_alert(cond_id, outcome, threshold)
                print(f"Alert added: {outcome} ±{threshold*100:.1f}%")

            elif action == "auto" and len(parts) >= 7:
                cond_id = parts[1]
                outcome = parts[2].upper()
                trigger = float(parts[3])
                direction = parts[4].lower()
                trade_action = parts[5].lower()
                amount = float(parts[6])
                limit_price = float(parts[7]) if len(parts) > 7 else None

                monitor.add_auto_trade(
                    cond_id, outcome, trigger, direction, trade_action, amount, limit_price
                )
                print(f"Auto-trade added: {trade_action} {outcome} when {direction} {trigger*100:.1f}%")

            elif action == "start":
                if loop_task is None or loop_task.done():
                    loop_task = asyncio.create_task(monitor.run())
                    print("Monitoring started.")
                else:
                    print("Already running.")

            elif action == "stop":
                monitor.stop()
                print("Monitoring stopped.")

            elif action == "status":
                print_json(monitor.status())

            else:
                print("Unknown command. Type 'quit' to exit.")

        except EOFError:
            break
        except Exception as e:
            print(f"Error: {e}")


async def cmd_monitor_config(
    client: PolymarketClient,
    alerter: SMSAlerter,
    config_file: str,
):
    """Run monitoring from config file."""
    with open(config_file) as f:
        cfg = json.load(f)

    monitor = MarketMonitor(client, alerter, poll_interval=cfg.get("poll_interval", 5.0))

    for m in cfg.get("markets", []):
        market = monitor.add_market(
            m["condition_id"],
            m["name"],
            m["yes_token_id"],
            m["no_token_id"],
        )

        for a in m.get("alerts", []):
            monitor.add_price_alert(
                m["condition_id"],
                a["outcome"],
                a["threshold"],
                TriggerDirection(a.get("direction", "both")),
                a.get("cooldown", 300),
            )

        for t in m.get("auto_trades", []):
            monitor.add_auto_trade(
                m["condition_id"],
                t["outcome"],
                t["trigger_price"],
                t["direction"],
                t["action"],
                t["amount"],
                t.get("limit_price"),
                t.get("one_shot", True),
            )

    print(f"Loaded {len(monitor.markets)} markets from {config_file}")

    def handle_sigint(sig, frame):
        monitor.stop()
        print("\nStopped.")
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_sigint)
    await monitor.run()


def main():
    parser = argparse.ArgumentParser(description="Polymarket Trading Bot")
    subparsers = parser.add_subparsers(dest="command", help="Commands")

    # Search
    sp = subparsers.add_parser("search", help="Search markets")
    sp.add_argument("query", help="Search query")

    # Price
    sp = subparsers.add_parser("price", help="Get token price")
    sp.add_argument("token_id", help="Token ID")

    # Order book
    sp = subparsers.add_parser("book", help="Show order book")
    sp.add_argument("token_id", help="Token ID")
    sp.add_argument("--depth", type=int, default=5, help="Book depth")

    # Buy
    sp = subparsers.add_parser("buy", help="Buy tokens")
    sp.add_argument("token_id", help="Token ID")
    sp.add_argument("amount", type=float, help="Dollar amount")
    sp.add_argument("--price", type=float, help="Limit price (optional)")

    # Sell
    sp = subparsers.add_parser("sell", help="Sell tokens")
    sp.add_argument("token_id", help="Token ID")
    sp.add_argument("amount", type=float, help="Token amount")
    sp.add_argument("--price", type=float, help="Limit price (optional)")

    # Positions
    subparsers.add_parser("positions", help="View positions")

    # Orders
    subparsers.add_parser("orders", help="View open orders")

    # Cancel
    sp = subparsers.add_parser("cancel", help="Cancel order(s)")
    sp.add_argument("order_id", help="Order ID or 'all'")

    # Monitor
    sp = subparsers.add_parser("monitor", help="Start market monitor")
    sp.add_argument("--config", help="Config file (JSON)")

    # Derive API key
    subparsers.add_parser("derive-key", help="Derive API key from private key (first time)")
    subparsers.add_parser("create-key", help="Create new API key (if derive fails)")

    # Opportunity scanner
    sp = subparsers.add_parser("scan", help="Scan for profitable opportunities")
    sp.add_argument("--hours", type=int, default=24, help="Max hours to expiry (default: 24)")
    sp.add_argument("--min-profit", type=float, default=0.03, help="Min profit %% (default: 0.03)")
    sp.add_argument("--top", type=int, default=5, help="Show top N opportunities (default: 5)")
    sp.add_argument("--auto-execute", action="store_true", help="Auto-execute recommendations")
    sp.add_argument("--risk", choices=["conservative", "moderate", "aggressive"], default="conservative")
    sp.add_argument("--amount", type=float, help="Fixed $ amount per position (overrides default sizing)")
    sp.add_argument("--tp", type=float, help="Take profit %% for executed positions (e.g., 0.03 for 3%%)")
    sp.add_argument("--sl", type=float, help="Stop loss %% for executed positions (e.g., 0.05 for 5%%)")

    # Profit Monitor CLI (pm)
    pm_parser = subparsers.add_parser("pm", help="Profit monitor management")
    pm_subparsers = pm_parser.add_subparsers(dest="pm_command", help="Profit monitor commands")

    # pm status
    pm_subparsers.add_parser("status", help="Show monitor status")

    # pm list
    pm_subparsers.add_parser("list", help="List all TP/SL configurations")

    # pm add
    pm_add = pm_subparsers.add_parser("add", help="Add a position to monitor")
    pm_add.add_argument("--token", dest="token_id", help="Token ID of the position")
    pm_add.add_argument("--tp", type=float, help="Take profit percentage (e.g., 0.03 for 3%%)")
    pm_add.add_argument("--sl", type=float, help="Stop loss percentage (e.g., 0.05 for 5%%)")

    # pm add-all
    pm_add_all = pm_subparsers.add_parser("add-all", help="Add all open positions to monitor")
    pm_add_all.add_argument("--tp", type=float, help="Take profit percentage (e.g., 0.03 for 3%%)")
    pm_add_all.add_argument("--sl", type=float, help="Stop loss percentage (e.g., 0.05 for 5%%)")
    pm_add_all.add_argument("--overwrite", action="store_true", help="Overwrite existing configs")

    # pm edit
    pm_edit = pm_subparsers.add_parser("edit", help="Edit a configuration")
    pm_edit.add_argument("config_id", help="Configuration ID")
    pm_edit.add_argument("--tp", type=float, help="New take profit %% (0 to remove)")
    pm_edit.add_argument("--sl", type=float, help="New stop loss %% (0 to remove)")
    pm_edit.add_argument("--enable", action="store_true", help="Enable this config")
    pm_edit.add_argument("--disable", action="store_true", help="Disable this config")

    # pm delete
    pm_del = pm_subparsers.add_parser("delete", help="Delete a configuration")
    pm_del.add_argument("config_id", help="Configuration ID")

    # pm delete-all
    pm_del_all = pm_subparsers.add_parser("delete-all", help="Delete all configurations")
    pm_del_all.add_argument("-y", "--yes", action="store_true", help="Skip confirmation prompt")

    # pm sell-all
    pm_sell_all = pm_subparsers.add_parser("sell-all", help="Sell all positions at market price")
    pm_sell_all.add_argument("-y", "--yes", action="store_true", help="Skip confirmation prompt")

    # pm start
    pm_start = pm_subparsers.add_parser("start", help="Start the profit monitor")
    pm_start.add_argument("-y", "--yes", action="store_true", help="Skip confirmation prompts")

    # pm stop
    pm_subparsers.add_parser("stop", help="Stop the profit monitor")

    # pm log
    pm_log = pm_subparsers.add_parser("log", help="Show monitor logs")
    pm_log.add_argument("-n", "--lines", type=int, default=50, help="Number of lines to show")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    # Initialize client
    client = PolymarketClient()
    alerter = SMSAlerter()

    # Execute command
    if args.command == "search":
        asyncio.run(cmd_search(client, args.query))

    elif args.command == "price":
        cmd_price(client, args.token_id)

    elif args.command == "book":
        cmd_book(client, args.token_id, args.depth)

    elif args.command == "buy":
        cmd_buy(client, args.token_id, args.amount, args.price)

    elif args.command == "sell":
        cmd_sell(client, args.token_id, args.amount, args.price)

    elif args.command == "positions":
        asyncio.run(cmd_positions(client))

    elif args.command == "orders":
        cmd_orders(client)

    elif args.command == "cancel":
        cmd_cancel(client, args.order_id)

    elif args.command == "monitor":
        if args.config:
            asyncio.run(cmd_monitor_config(client, alerter, args.config))
        else:
            asyncio.run(cmd_monitor_interactive(client, alerter))

    elif args.command == "derive-key":
        client.derive_api_key()

    elif args.command == "scan":
        asyncio.run(cmd_scan(client, args))

    elif args.command == "create-key":
        creds = client.create_api_key()
        print(f"API Key: {creds.api_key}")
        print(f"API Secret: {creds.api_secret}")
        print(f"API Passphrase: {creds.api_passphrase}")

    elif args.command == "pm":
        manager = get_manager()

        if args.pm_command == "status":
            asyncio.run(cmd_pm_status(client, manager))

        elif args.pm_command == "list":
            asyncio.run(cmd_pm_list(client, manager))

        elif args.pm_command == "add":
            asyncio.run(cmd_pm_add(client, manager, args))

        elif args.pm_command == "add-all":
            asyncio.run(cmd_pm_add_all(client, manager, args))

        elif args.pm_command == "edit":
            asyncio.run(cmd_pm_edit(manager, args))

        elif args.pm_command == "delete":
            asyncio.run(cmd_pm_delete(manager, args))

        elif args.pm_command == "delete-all":
            asyncio.run(cmd_pm_delete_all(manager, args))

        elif args.pm_command == "sell-all":
            asyncio.run(cmd_pm_sell_all(client, manager, args))

        elif args.pm_command == "start":
            asyncio.run(cmd_pm_start(client, manager, args))

        elif args.pm_command == "stop":
            asyncio.run(cmd_pm_stop(manager))

        elif args.pm_command == "log":
            asyncio.run(cmd_pm_log(manager, args))

        else:
            pm_parser.print_help()


if __name__ == "__main__":
    main()
