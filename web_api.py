#!/usr/bin/env python3
"""
Web API for Polymarket Trading Bot.
Provides REST endpoints for the web UI.
"""

import asyncio
import json
import time
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
from functools import wraps
import os
from datetime import datetime
from pathlib import Path

from polymarket_client import PolymarketClient
from opportunity_scanner import OpportunityScanner
from scanner_config import ScannerConfig
from monitor_config import get_manager, LOG_FILE, CONFIG_DIR
from copy_trading_config import get_ct_manager, CT_LOG_FILE, CT_CONFIG_DIR, CT_DETECTED_TRADES_FILE, CT_EXECUTED_TRADES_FILE
from log_manager import log_manager, get_logger
from scan_history import scan_history
from api_guard import api_guard

app = Flask(__name__, static_folder='web_ui')
CORS(app)

# Global client instance
client = PolymarketClient()

# P&L History file
PNL_HISTORY_FILE = CONFIG_DIR / "pnl_history.json"


def load_pnl_history() -> list:
    """Load P&L history from file."""
    if not PNL_HISTORY_FILE.exists():
        return []
    try:
        with open(PNL_HISTORY_FILE, 'r') as f:
            return json.load(f)
    except:
        return []


def save_pnl_history(history: list):
    """Save P&L history to file."""
    # Keep only last 1000 data points
    history = history[-1000:]
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(PNL_HISTORY_FILE, 'w') as f:
        json.dump(history, f)


def record_pnl_point(pnl: float, portfolio_value: float, cash: float):
    """Record a P&L data point."""
    history = load_pnl_history()
    now = datetime.now()

    # Only record if 5+ minutes since last point (avoid too many points)
    if history:
        last_time = datetime.fromisoformat(history[-1]['timestamp'])
        if (now - last_time).total_seconds() < 300:
            return

    history.append({
        'timestamp': now.isoformat(),
        'pnl': pnl,
        'portfolio_value': portfolio_value,
        'cash': cash,
        'total': portfolio_value + cash,
    })
    save_pnl_history(history)


def async_route(f):
    """Decorator to run async functions in Flask routes."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        return asyncio.run(f(*args, **kwargs))
    return wrapper


# ============== Positions ==============

@app.route('/api/positions')
@async_route
async def get_positions():
    """Get all current positions."""
    try:
        positions = await client.get_positions()

        # Filter out inactive/resolved positions (size <= 0 or currentValue <= 0)
        active_positions = [
            p for p in (positions or [])
            if float(p.get('size', 0)) > 0 and float(p.get('currentValue', 0)) > 0
        ]

        return jsonify({
            'success': True,
            'positions': active_positions
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/balance')
@async_route
async def get_balance():
    """Get account balance and portfolio value."""
    try:
        import aiohttp

        proxy_wallet = client.proxy_wallet or client.address

        # Get USDC balance on-chain (Polygon USDC contract)
        usdc_contract = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
        # balanceOf(address) function selector + address padded to 32 bytes
        wallet_padded = proxy_wallet[2:].lower().zfill(64)
        call_data = f"0x70a08231{wallet_padded}"

        async with aiohttp.ClientSession() as session:
            # Get USDC balance from Polygon RPC
            async with session.post(
                "https://polygon-rpc.com",
                json={
                    "jsonrpc": "2.0",
                    "method": "eth_call",
                    "params": [{"to": usdc_contract, "data": call_data}, "latest"],
                    "id": 1
                }
            ) as resp:
                data = await resp.json()
                balance_hex = data.get('result', '0x0')
                cash_available = int(balance_hex, 16) / 1e6  # USDC has 6 decimals

        # Get positions value and P&L
        positions = await client.get_positions()
        invested = sum(float(p.get('currentValue', 0)) for p in positions) if positions else 0
        total_pnl = sum(float(p.get('cashPnl', 0)) for p in positions) if positions else 0

        # Total portfolio = cash + invested positions
        portfolio_value = cash_available + invested

        # Record P&L history point
        record_pnl_point(total_pnl, invested, cash_available)

        return jsonify({
            'success': True,
            'portfolio_value': portfolio_value,
            'invested': invested,
            'cash_available': cash_available,
            'total_pnl': total_pnl,
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/pnl-history')
@async_route
async def get_pnl_history():
    """Get P&L history from Polymarket trade activity."""
    try:
        import aiohttp

        hours = request.args.get('hours', 24, type=int)
        proxy_wallet = client.proxy_wallet or client.address

        # Fetch trade activity from Polymarket
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://data-api.polymarket.com/activity",
                params={"user": proxy_wallet, "limit": 500}
            ) as resp:
                if resp.status != 200:
                    return jsonify({'success': False, 'error': 'Failed to fetch activity'}), 500
                activities = await resp.json()

        if not activities:
            return jsonify({'success': True, 'history': []})

        # Filter by time range
        cutoff = datetime.now().timestamp() - (hours * 3600) if hours > 0 else 0
        activities = [a for a in activities if a.get('timestamp', 0) >= cutoff]

        # Sort by timestamp ascending (oldest first)
        activities.sort(key=lambda x: x.get('timestamp', 0))

        # Track positions and calculate cumulative P&L
        positions = {}  # asset -> {size, cost_basis}
        history = []
        cumulative_realized_pnl = 0

        for activity in activities:
            if activity.get('type') != 'TRADE':
                continue

            timestamp = activity.get('timestamp', 0)
            asset = activity.get('asset', '')
            side = activity.get('side', '')
            size = float(activity.get('size', 0))
            usdc_size = float(activity.get('usdcSize', 0))
            price = float(activity.get('price', 0))

            if asset not in positions:
                positions[asset] = {'size': 0, 'cost_basis': 0, 'avg_price': 0}

            pos = positions[asset]

            if side == 'BUY':
                # Add to position
                total_cost = pos['cost_basis'] + usdc_size
                total_size = pos['size'] + size
                pos['size'] = total_size
                pos['cost_basis'] = total_cost
                pos['avg_price'] = total_cost / total_size if total_size > 0 else 0
            elif side == 'SELL':
                # Calculate realized P&L
                if pos['size'] > 0:
                    # Cost basis for shares sold
                    sell_cost_basis = pos['avg_price'] * size
                    realized_pnl = usdc_size - sell_cost_basis
                    cumulative_realized_pnl += realized_pnl

                    # Update position
                    pos['size'] = max(0, pos['size'] - size)
                    if pos['size'] <= 0:
                        pos['cost_basis'] = 0
                        pos['avg_price'] = 0
                    else:
                        pos['cost_basis'] = pos['avg_price'] * pos['size']

            # Record history point
            dt = datetime.fromtimestamp(timestamp)
            history.append({
                'timestamp': dt.isoformat(),
                'pnl': round(cumulative_realized_pnl, 2),
                'type': 'realized'
            })

        # Add current unrealized P&L from open positions
        current_positions = await client.get_positions()
        unrealized_pnl = sum(float(p.get('cashPnl', 0)) for p in current_positions) if current_positions else 0
        total_pnl = cumulative_realized_pnl + unrealized_pnl

        # Add final point with current total P&L
        if history:
            history.append({
                'timestamp': datetime.now().isoformat(),
                'pnl': round(total_pnl, 2),
                'type': 'total'
            })

        return jsonify({
            'success': True,
            'history': history,
            'realized_pnl': round(cumulative_realized_pnl, 2),
            'unrealized_pnl': round(unrealized_pnl, 2),
            'total_pnl': round(total_pnl, 2),
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ============== Profit Monitor ==============

@app.route('/api/pm/status')
def pm_status():
    """Get profit monitor status."""
    try:
        manager = get_manager()
        pid = manager.get_monitor_pid()
        configs = manager.list_all()
        enabled = [c for c in configs if c.enabled]

        return jsonify({
            'success': True,
            'running': pid is not None,
            'pid': pid,
            'total_configs': len(configs),
            'enabled_configs': len(enabled)
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/pm/configs')
def pm_configs():
    """Get all PM configurations with current prices."""
    try:
        manager = get_manager()
        configs = manager.list_all()

        result = []
        for c in configs:
            tp = c.get_tp_target()
            sl = c.get_sl_target()

            # Get current price
            try:
                mid = client.get_midpoint_price(c.token_id)
                cur_pnl = ((mid / c.entry_price) - 1) * 100 if c.entry_price > 0 else 0
            except:
                mid = None
                cur_pnl = None

            # Calculate gain/loss percentages
            tp_gain_pct = ((tp / c.entry_price) - 1) * 100 if tp and c.entry_price > 0 else None
            sl_loss_pct = (1 - (sl / c.entry_price)) * 100 if sl and c.entry_price > 0 else None

            result.append({
                'id': c.id,
                'token_id': c.token_id,
                'name': c.name,
                'description': getattr(c, 'description', '') or '',
                'slug': getattr(c, 'slug', '') or '',
                'side': c.side,
                'shares': c.shares,
                'entry_price': c.entry_price,
                'current_price': mid,
                'current_pnl_pct': cur_pnl,
                'take_profit_price': tp,
                'take_profit_pct': tp_gain_pct,
                'stop_loss_price': sl,
                'stop_loss_pct': sl_loss_pct,
                'enabled': c.enabled,
            })

        return jsonify({
            'success': True,
            'configs': result
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/pm/config/<config_id>', methods=['GET'])
def pm_get_config(config_id):
    """Get a specific PM config."""
    try:
        manager = get_manager()
        config = manager.get(config_id)
        if not config:
            return jsonify({'success': False, 'error': 'Config not found'}), 404

        return jsonify({
            'success': True,
            'config': {
                'id': config.id,
                'token_id': config.token_id,
                'name': config.name,
                'side': config.side,
                'shares': config.shares,
                'entry_price': config.entry_price,
                'take_profit_price': config.get_tp_target(),
                'stop_loss_price': config.get_sl_target(),
                'enabled': config.enabled,
            }
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/pm/config/<config_id>', methods=['PUT'])
def pm_update_config(config_id):
    """Update a PM config (TP/SL)."""
    try:
        manager = get_manager()
        config = manager.get(config_id)
        if not config:
            return jsonify({'success': False, 'error': 'Config not found'}), 404

        data = request.json
        updates = {}

        if 'take_profit_pct' in data:
            updates['take_profit_pct'] = data['take_profit_pct']
        if 'stop_loss_pct' in data:
            updates['stop_loss_pct'] = data['stop_loss_pct']
        if 'enabled' in data:
            updates['enabled'] = data['enabled']

        if updates:
            config = manager.update(config_id, **updates)
            _restart_monitor_if_running(manager)

        return jsonify({
            'success': True,
            'config': {
                'id': config.id,
                'take_profit_price': config.get_tp_target(),
                'stop_loss_price': config.get_sl_target(),
                'enabled': config.enabled,
            }
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/pm/config/<config_id>', methods=['DELETE'])
def pm_delete_config(config_id):
    """Delete a PM config."""
    try:
        manager = get_manager()
        config = manager.get(config_id)
        if not config:
            return jsonify({'success': False, 'error': 'Config not found'}), 404

        manager.delete(config_id)
        _restart_monitor_if_running(manager)

        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/pm/add', methods=['POST'])
@async_route
async def pm_add_config():
    """Add a position to PM with TP/SL."""
    try:
        data = request.json
        token_id = data.get('token_id')
        tp_pct = data.get('take_profit_pct')
        sl_pct = data.get('stop_loss_pct')

        if not token_id:
            return jsonify({'success': False, 'error': 'token_id required'}), 400

        # Get position info
        positions = await client.get_positions()
        pos = next((p for p in positions if p.get('asset') == token_id), None)

        if not pos:
            return jsonify({'success': False, 'error': 'Position not found'}), 404

        manager = get_manager()

        # Check if already exists
        existing = manager.get_by_token(token_id)
        if existing:
            return jsonify({'success': False, 'error': f'Config already exists: {existing.id}'}), 400

        config = manager.add(
            token_id=token_id,
            name=pos.get('title', 'Unknown')[:50],
            side=pos.get('outcome', 'Unknown'),
            shares=float(pos.get('size', 0)),
            entry_price=float(pos.get('avgPrice', 0)),
            take_profit_pct=tp_pct,
            stop_loss_pct=sl_pct,
        )

        _restart_monitor_if_running(manager)

        return jsonify({
            'success': True,
            'config': {
                'id': config.id,
                'name': config.name,
                'take_profit_price': config.get_tp_target(),
                'stop_loss_price': config.get_sl_target(),
            }
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/pm/add-all', methods=['POST'])
@async_route
async def pm_add_all():
    """Add all positions to PM with TP/SL."""
    try:
        data = request.json
        tp_pct = data.get('take_profit_pct')
        sl_pct = data.get('stop_loss_pct')
        overwrite = data.get('overwrite', False)

        if not tp_pct and not sl_pct:
            return jsonify({'success': False, 'error': 'Must specify tp or sl'}), 400

        positions = await client.get_positions()
        if not positions:
            return jsonify({'success': False, 'error': 'No positions found'}), 404

        manager = get_manager()
        added = 0
        updated = 0
        skipped = 0

        for p in positions:
            token_id = p.get('asset', '')
            existing = manager.get_by_token(token_id)

            if existing:
                if overwrite:
                    updates = {}
                    if tp_pct:
                        updates['take_profit_pct'] = tp_pct
                    if sl_pct:
                        updates['stop_loss_pct'] = sl_pct
                    manager.update(existing.id, **updates)
                    updated += 1
                else:
                    skipped += 1
            else:
                manager.add(
                    token_id=token_id,
                    name=p.get('title', 'Unknown')[:50],
                    side=p.get('outcome', 'Unknown'),
                    shares=float(p.get('size', 0)),
                    entry_price=float(p.get('avgPrice', 0)),
                    take_profit_pct=tp_pct,
                    stop_loss_pct=sl_pct,
                )
                added += 1

        if added > 0 or updated > 0:
            _restart_monitor_if_running(manager)

        return jsonify({
            'success': True,
            'added': added,
            'updated': updated,
            'skipped': skipped,
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/pm/delete-all', methods=['DELETE'])
def pm_delete_all():
    """Delete all PM configs."""
    try:
        manager = get_manager()
        configs = manager.list_all()

        was_running = manager.is_monitor_running()
        if was_running:
            _stop_monitor(manager)

        count = 0
        for c in configs:
            manager.delete(c.id)
            count += 1

        return jsonify({
            'success': True,
            'deleted': count
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/pm/start', methods=['POST'])
def pm_start():
    """Start the profit monitor."""
    try:
        manager = get_manager()

        if manager.is_monitor_running():
            return jsonify({'success': False, 'error': 'Already running'}), 400

        configs = manager.list_enabled()
        if not configs:
            return jsonify({'success': False, 'error': 'No enabled configs'}), 400

        # Start monitor
        import subprocess
        monitor_script = os.path.join(os.path.dirname(__file__), 'profit_monitor.py')
        log_file = str(LOG_FILE)
        cmd = f"nohup python -u {monitor_script} >> {log_file} 2>&1 &"
        subprocess.Popen(cmd, shell=True, start_new_session=True)

        import time
        time.sleep(2)

        if manager.is_monitor_running():
            return jsonify({
                'success': True,
                'pid': manager.get_monitor_pid()
            })
        else:
            return jsonify({'success': False, 'error': 'Failed to start'}), 500
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/pm/stop', methods=['POST'])
def pm_stop():
    """Stop the profit monitor."""
    try:
        manager = get_manager()
        pid = manager.get_monitor_pid()

        if not pid:
            return jsonify({'success': False, 'error': 'Not running'}), 400

        _stop_monitor(manager)

        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/pm/logs')
def pm_logs():
    """Get recent monitor logs."""
    try:
        lines = request.args.get('lines', 50, type=int)

        if not LOG_FILE.exists():
            return jsonify({'success': True, 'logs': []})

        with open(LOG_FILE, 'r') as f:
            all_lines = f.readlines()
            # Deduplicate consecutive lines (monitor outputs twice sometimes)
            deduped = []
            prev = None
            for line in all_lines[-lines*2:]:
                if line != prev:
                    deduped.append(line.rstrip())
                prev = line

        return jsonify({
            'success': True,
            'logs': deduped[-lines:]
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ============== Real-time Logging System ==============

@app.route('/api/logs/channels')
def get_log_channels():
    """Get available log channels."""
    return jsonify({
        'success': True,
        'channels': log_manager.get_all_channels()
    })


@app.route('/api/logs/<channel>')
def get_channel_logs(channel):
    """Get logs from a specific channel."""
    try:
        count = request.args.get('count', 100, type=int)
        since = request.args.get('since', 0, type=float)

        if since > 0:
            logs = log_manager.get_logs_since(channel, since)
        else:
            logs = log_manager.get_logs(channel, count)

        return jsonify({
            'success': True,
            'channel': channel,
            'logs': logs,
            'timestamp': time.time()
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/logs/<channel>/clear', methods=['POST'])
def clear_channel_logs(channel):
    """Clear logs for a channel."""
    try:
        log_manager.clear_channel(channel)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ============== API Guard (Credit Status) ==============

@app.route('/api/guard/status')
def get_api_guard_status():
    """Check if API is blocked due to credit issues."""
    return jsonify({
        'success': True,
        'blocked': api_guard.is_blocked(),
        'error_message': api_guard.get_error_message(),
    })


@app.route('/api/guard/reset', methods=['POST'])
def reset_api_guard():
    """Reset the API guard (after adding credits)."""
    api_guard.reset()
    return jsonify({
        'success': True,
        'message': 'API guard reset. API calls will be attempted again.',
    })


# ============== Market Search ==============

@app.route('/api/search')
@async_route
async def search_markets():
    """Search for markets by text query with pagination. Uses Gamma API pre-sorted by volume."""
    try:
        import aiohttp
        from datetime import datetime

        query = request.args.get('q', '').strip()
        limit = request.args.get('limit', 25, type=int)
        page = request.args.get('page', 1, type=int)

        if not query:
            return jsonify({'success': False, 'error': 'Search query required'}), 400

        # Split query into words for matching
        query_lower = query.lower()
        query_words = [w.strip() for w in query_lower.split() if w.strip()]

        def matches_query(text):
            """Check if text contains all query words."""
            if not text:
                return False
            text_lower = text.lower()
            return all(word in text_lower for word in query_words)

        all_results = []
        seen_ids = set()
        now = datetime.now()

        def process_market(m, event_title=None):
            """Process a market dict and return result dict if it matches, else None."""
            question = m.get("question") or ""
            description = m.get("description") or ""

            if not (matches_query(question) or matches_query(description)):
                return None

            # Skip duplicates
            market_id = m.get('id', '')
            if market_id in seen_ids:
                return None
            seen_ids.add(market_id)

            # Filter out ended markets
            end_date_str = m.get('endDate')
            if end_date_str:
                try:
                    end_date = datetime.fromisoformat(end_date_str.replace('Z', '+00:00'))
                    end_date = end_date.replace(tzinfo=None)
                    if end_date < now:
                        return None
                except:
                    pass

            # Skip closed markets
            if m.get('closed', False):
                return None

            # Parse token IDs
            clob_token_ids = m.get('clobTokenIds', '[]')
            try:
                token_ids = json.loads(clob_token_ids) if isinstance(clob_token_ids, str) else (clob_token_ids or [])
            except:
                token_ids = []

            # Parse prices
            outcome_prices = m.get('outcomePrices', '[]')
            try:
                prices = json.loads(outcome_prices) if isinstance(outcome_prices, str) else (outcome_prices or [])
            except:
                prices = []

            yes_price = float(prices[0]) if len(prices) > 0 else 0.5
            no_price = float(prices[1]) if len(prices) > 1 else 0.5

            # Build tokens array
            tokens = []
            if len(token_ids) > 0:
                tokens.append({'token_id': str(token_ids[0]), 'outcome': 'Yes', 'price': yes_price})
            if len(token_ids) > 1:
                tokens.append({'token_id': str(token_ids[1]), 'outcome': 'No', 'price': no_price})

            return {
                'condition_id': m.get('conditionId', ''),
                'question': question,
                'event_title': event_title or m.get('groupItemTitle') or m.get('title') or '',
                'slug': m.get('slug', ''),
                'end_date': end_date_str,
                'closed': m.get('closed', False),
                'yes_price': yes_price,
                'no_price': no_price,
                'volume': float(m.get('volume', 0) or 0),
                'liquidity': float(m.get('liquidity', 0) or 0),
                'tokens': tokens,
            }

        async with aiohttp.ClientSession() as session:
            # Fetch from both /markets and /events endpoints
            # Events contain grouped markets that don't appear in /markets

            # 1. Fetch from /events endpoint (contains grouped markets)
            offset = 0
            batch_size = 500
            max_fetched = 3000  # Need to fetch many events since API doesn't sort by volume properly

            while offset < max_fetched:
                params = {
                    'closed': 'false',
                    'limit': batch_size,
                    'offset': offset
                }

                async with session.get("https://gamma-api.polymarket.com/events", params=params) as resp:
                    if resp.status != 200:
                        break
                    events_data = await resp.json()

                if not events_data:
                    break

                for event in events_data:
                    event_title = event.get('title', '')
                    # Check if event title/description matches
                    event_matches = matches_query(event_title) or matches_query(event.get('description', ''))

                    # Process markets within this event
                    for m in event.get('markets', []):
                        # If event matches, include all its markets; otherwise check each market
                        if event_matches or matches_query(m.get('question', '')) or matches_query(m.get('description', '')):
                            result = process_market(m, event_title)
                            if result:
                                all_results.append(result)

                offset += batch_size
                if len(events_data) < batch_size:
                    break

            # 2. Also fetch from /markets endpoint for non-grouped markets
            offset = 0
            batch_size = 500
            max_fetched = 2000

            while offset < max_fetched:
                params = {
                    'closed': 'false',
                    'limit': batch_size,
                    'offset': offset
                }

                async with session.get("https://gamma-api.polymarket.com/markets", params=params) as resp:
                    if resp.status != 200:
                        break
                    markets_data = await resp.json()

                if not markets_data:
                    break

                for m in markets_data:
                    result = process_market(m)
                    if result:
                        all_results.append(result)

                offset += batch_size

                # If we got fewer results than batch_size, we've reached the end
                if len(markets_data) < batch_size:
                    break

        # Sort by volume descending (highest first)
        all_results.sort(key=lambda m: m.get('volume', 0) or 0, reverse=True)

        # Apply pagination
        start_idx = (page - 1) * limit
        end_idx = start_idx + limit
        paginated_results = all_results[start_idx:end_idx]
        total_results = len(all_results)
        total_pages = (total_results + limit - 1) // limit if total_results > 0 else 1

        return jsonify({
            'success': True,
            'markets': paginated_results,
            'query': query,
            'page': page,
            'limit': limit,
            'total_results': total_results,
            'total_pages': total_pages,
            'has_more': page < total_pages,
        })

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ============== Opportunities Scanner ==============

@app.route('/api/scan')
@async_route
async def scan_opportunities():
    """Scan for trading opportunities."""
    scanner_log = get_logger('scanner')
    try:
        start_time = time.time()

        hours = request.args.get('hours', 48, type=int)  # Default 48 hours
        top = request.args.get('top', 20, type=int)  # Show more by default
        risk = request.args.get('risk', 'moderate')  # Default to moderate for more results
        max_ai = request.args.get('max_ai', 10, type=int)  # Limit AI analysis to control costs

        scanner_log.info(f"Starting scan: hours={hours}, top={top}, risk={risk}, max_ai={max_ai}")

        # Risk profile determines filtering thresholds automatically
        scanner_config = ScannerConfig(
            max_hours_to_expiry=hours,
            risk_mode=risk,
            max_ai_analysis=max_ai,  # Limit how many markets get Claude analysis
        )

        scanner = OpportunityScanner(client, scanner_config)

        # Get scan stats from scanner
        scanner_log.info("Running opportunity scan...")
        scan_result = await scanner.scan_with_stats()
        elapsed = time.time() - start_time
        scanner_log.info(f"Scan completed in {elapsed:.1f}s")

        opportunities = scan_result.get('opportunities', [])
        stats = scan_result.get('stats', {})
        scanner_log.info(f"Found {len(opportunities)} opportunities")

        result = []
        for opp in opportunities[:top]:
            # Construct Polymarket URL
            slug = getattr(opp, 'slug', '') or ''
            polymarket_url = f"https://polymarket.com/event/{slug}" if slug else ""

            result.append({
                'token_id': opp.token_id,
                'condition_id': opp.condition_id,
                'title': opp.title,
                'event_title': opp.event_title,
                'slug': slug,
                'polymarket_url': polymarket_url,
                'description': getattr(opp, 'description', '') or '',
                'end_date': opp.end_date.isoformat() if opp.end_date else None,
                'hours_to_expiry': opp.hours_to_expiry,
                'recommended_side': opp.recommended_side,
                'entry_price': opp.entry_price,
                'expected_resolution': opp.expected_resolution,
                'expected_profit_pct': opp.expected_profit_pct,
                'confidence_score': opp.confidence_score,
                'risk_score': opp.risk_score,
                'liquidity': opp.liquidity,
                'spread': opp.spread,
                'volume_24h': opp.volume_24h,
                'news_summary': opp.news_summary,
                'recommended_amount': opp.recommended_amount,
                'potential_profit': opp.potential_profit,
                # Claude AI analysis
                'claude_probability': opp.claude_probability,
                'claude_confidence': opp.claude_confidence,
                'claude_recommendation': opp.claude_recommendation,
                'claude_reasoning': opp.claude_reasoning,
                'claude_edge': opp.claude_edge,
                'claude_risk_factors': opp.claude_risk_factors,
                # Historical analysis
                'price_trend': opp.price_trend,
                'price_volatility': opp.price_volatility,
                # Cross-market correlation
                'related_markets': [
                    {'question': m.get('question', ''), 'yes_price': m.get('yes_price', 0.5)}
                    for m in (opp.related_markets or [])[:3]
                ],
                'correlation_risk': opp.correlation_risk,
                # Web research
                'web_context': opp.web_context,
                'event_status': opp.event_status,
                # Deep research
                'deep_research_summary': opp.deep_research_summary,
                'deep_research_probability': opp.deep_research_probability,
                'deep_research_quality': opp.deep_research_quality,
                'key_facts': opp.key_facts,
                'recent_news': opp.recent_news,
                'expert_opinions': opp.expert_opinions,
                'contrary_evidence': opp.contrary_evidence,
                'research_sentiment': opp.research_sentiment,
                # Real-time facts
                'research_facts': opp.research_facts,
                'research_status': opp.research_status,
                'research_progress': opp.research_progress,
                'facts_quality': opp.facts_quality,
                'facts_gathered_at': opp.facts_gathered_at,
                # Triage status
                'triage_status': opp.triage_status,
                'triage_reasons': opp.triage_reasons,
                # AI analysis status
                'ai_analysis_skipped': opp.ai_analysis_skipped,
                'preliminary_score': opp.preliminary_score,
            })

        scanner_log.info(f"Returning {len(result)} opportunities to frontend")

        # Save to scan history with retention based on max_hours
        scan_id = scan_history.save_scan(
            scan_type='quick',
            parameters={
                'hours': hours,
                'risk': risk,
                'top': top,
                'max_ai': max_ai,
            },
            retention_hours=hours,  # Retain for same duration as scan window
            opportunities=result,
            stats=stats,
        )
        scanner_log.info(f"Saved scan to history: {scan_id}")

        return jsonify({
            'success': True,
            'opportunities': result,
            'total_found': len(opportunities),
            'stats': stats,
            'scan_id': scan_id,
        })
    except Exception as e:
        import traceback
        scanner_log.error(f"Scan failed: {str(e)}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/deep-research', methods=['POST'])
@async_route
async def deep_research_market():
    """Perform deep research on a specific market."""
    research_log = get_logger('deep_research')
    try:
        from deep_researcher import DeepMarketAnalyzer

        data = request.json
        condition_id = data.get('condition_id')
        title = data.get('title', '')
        description = data.get('description', '')
        event_title = data.get('event_title', '')
        end_date = data.get('end_date', '')
        yes_price = data.get('yes_price', 0.5)

        research_log.info(f"Starting deep research for: {title[:50]}...")

        if not condition_id and not title:
            research_log.error("Missing condition_id or title")
            return jsonify({'success': False, 'error': 'condition_id or title required'}), 400

        # If we have condition_id but not title, fetch market info
        if condition_id and not title:
            research_log.info(f"Fetching market info for condition_id: {condition_id}")
            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"https://gamma-api.polymarket.com/markets/{condition_id}"
                ) as resp:
                    if resp.status == 200:
                        market = await resp.json()
                        title = market.get('question', '')
                        description = market.get('description', '')
                        event_title = market.get('groupItemTitle', '')
                        end_date = market.get('endDate', '')
                        # Parse prices
                        prices_str = market.get('outcomePrices', '[]')
                        prices = json.loads(prices_str) if isinstance(prices_str, str) else prices_str
                        yes_price = float(prices[0]) if prices else 0.5
                        research_log.info(f"Market info fetched: {title[:50]}...")

        # Run deep research
        research_log.info("Starting Claude analysis with web search...")
        analyzer = DeepMarketAnalyzer()
        result = await analyzer.analyze_with_research(
            condition_id=condition_id or 'manual',
            title=title,
            description=description,
            event_title=event_title,
            end_date=end_date,
            yes_price=yes_price,
            no_price=1.0 - yes_price,
        )

        research_log.info(f"Research complete. Recommendation: {result.get('recommendation', 'SKIP')}, Edge: {result.get('edge', 0):.1%}")
        return jsonify({
            'success': True,
            'research': result.get('research', {}),
            'analysis': result.get('analysis', {}),
            'final_probability': result.get('final_probability', 0.5),
            'final_confidence': result.get('final_confidence', 0.5),
            'recommendation': result.get('recommendation', 'SKIP'),
            'edge': result.get('edge', 0),
            'reasoning': result.get('reasoning', ''),
        })

    except Exception as e:
        import traceback
        research_log.error(f"Deep research failed: {str(e)}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/scan-deep')
@async_route
async def scan_with_deep_research():
    """Scan for opportunities with deep research enabled."""
    deep_log = get_logger('deep_research')
    scanner_log = get_logger('scanner')
    try:
        hours = request.args.get('hours', 48, type=int)
        top = request.args.get('top', 10, type=int)  # More for deep research
        risk = request.args.get('risk', 'moderate')
        max_ai = request.args.get('max_ai', 10, type=int)

        deep_log.info(f"Starting deep research scan: hours={hours}, top={top}, risk={risk}")
        scanner_log.info(f"Starting deep research scan: hours={hours}, top={top}, risk={risk}")

        # Create config with deep research enabled
        scanner_config = ScannerConfig(
            max_hours_to_expiry=hours,
            risk_mode=risk,
            enable_deep_research=True,
            deep_research_top_n=top,
            max_ai_analysis=max_ai,
        )

        scanner = OpportunityScanner(client, scanner_config)
        deep_log.info("Running scanner with deep research enabled...")
        scan_result = await scanner.scan_with_stats()
        opportunities = scan_result.get('opportunities', [])
        stats = scan_result.get('stats', {})

        deep_log.info(f"Scan complete: {len(opportunities)} opportunities, {stats.get('deep_researched', 0)} deep researched")

        result = []
        for opp in opportunities[:top]:
            result.append({
                'token_id': opp.token_id,
                'condition_id': opp.condition_id,
                'title': opp.title,
                'event_title': opp.event_title,
                'end_date': opp.end_date.isoformat() if opp.end_date else None,
                'hours_to_expiry': opp.hours_to_expiry,
                'recommended_side': opp.recommended_side,
                'entry_price': opp.entry_price,
                'expected_profit_pct': opp.expected_profit_pct,
                'confidence_score': opp.confidence_score,
                'risk_score': opp.risk_score,
                'liquidity': opp.liquidity,
                'volume_24h': opp.volume_24h,
                # Claude analysis (now backed by deep research)
                'claude_probability': opp.claude_probability,
                'claude_confidence': opp.claude_confidence,
                'claude_recommendation': opp.claude_recommendation,
                'claude_reasoning': opp.claude_reasoning,
                'claude_edge': opp.claude_edge,
                # Deep research results
                'deep_research': {
                    'summary': opp.deep_research_summary,
                    'probability': opp.deep_research_probability,
                    'quality': opp.deep_research_quality,
                    'key_facts': opp.key_facts,
                    'recent_news': opp.recent_news,
                    'expert_opinions': opp.expert_opinions,
                    'contrary_evidence': opp.contrary_evidence,
                    'sentiment': opp.research_sentiment,
                },
                'event_status': opp.event_status,
                'recommended_amount': opp.recommended_amount,
            })

        # Save to scan history with retention based on max_hours
        scan_id = scan_history.save_scan(
            scan_type='deep',
            parameters={
                'hours': hours,
                'risk': risk,
                'top': top,
                'max_ai': max_ai,
            },
            retention_hours=hours,  # Retain for same duration as scan window
            opportunities=result,
            stats=stats,
        )
        deep_log.info(f"Saved deep scan to history: {scan_id}")

        return jsonify({
            'success': True,
            'opportunities': result,
            'total_found': len(opportunities),
            'stats': stats,
            'scan_id': scan_id,
        })
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


# ============== Scan History ==============

@app.route('/api/scan/history')
def get_scan_history():
    """List all saved scans."""
    try:
        scans = scan_history.list_scans()
        return jsonify({
            'success': True,
            'scans': scans,
            'count': len(scans),
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/scan/history/<scan_id>')
def get_scan_by_id(scan_id):
    """Get a specific scan by ID."""
    try:
        record = scan_history.get_scan(scan_id)
        if not record:
            return jsonify({'success': False, 'error': 'Scan not found'}), 404

        return jsonify({
            'success': True,
            'scan': {
                'scan_id': record.scan_id,
                'timestamp': record.timestamp,
                'scan_type': record.scan_type,
                'parameters': record.parameters,
                'retention_hours': record.retention_hours,
                'expires_at': record.expires_at,
                'opportunities_count': record.opportunities_count,
                'stats': record.stats,
                'opportunities': record.opportunities,
            },
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/scan/history/<scan_id>', methods=['DELETE'])
def delete_scan(scan_id):
    """Delete a specific scan."""
    try:
        if scan_history.delete_scan(scan_id):
            return jsonify({'success': True})
        return jsonify({'success': False, 'error': 'Scan not found'}), 404
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/enhance-opportunity', methods=['POST'])
@async_route
async def enhance_opportunity():
    """Enhance a single opportunity with AI analysis."""
    enhance_log = get_logger('scanner')
    try:
        data = request.json
        condition_id = data.get('condition_id')
        token_id = data.get('token_id')
        title = data.get('title', '')
        event_title = data.get('event_title', '')
        entry_price = data.get('entry_price', 0.5)
        recommended_side = data.get('recommended_side', 'YES')
        hours_to_expiry = data.get('hours_to_expiry', 24)

        if not condition_id or not token_id:
            return jsonify({'success': False, 'error': 'condition_id and token_id required'}), 400

        enhance_log.info(f"Enhancing opportunity: {title[:50]}...")

        # Import and initialize MarketAnalyzer
        from market_analyzer import MarketAnalyzer
        analyzer = MarketAnalyzer()

        # Run Claude analysis
        enhance_log.info("Running Claude AI analysis...")
        analysis = await analyzer.analyze_market(
            condition_id=condition_id,
            title=title,
            event_title=event_title,
            yes_price=entry_price if recommended_side == 'YES' else 1 - entry_price,
            hours_to_expiry=hours_to_expiry
        )

        if analysis:
            enhance_log.info(f"AI Analysis complete: {analysis.recommendation}")
            return jsonify({
                'success': True,
                'enhanced': True,
                'claude_probability': analysis.probability,
                'claude_confidence': analysis.confidence,
                'claude_recommendation': analysis.recommendation,
                'claude_reasoning': analysis.reasoning,
                'claude_edge': analysis.edge_estimate / 100,  # Convert to decimal
                'claude_risk_factors': analysis.risk_factors if hasattr(analysis, 'risk_factors') else [],
            })
        else:
            enhance_log.warning("AI analysis returned no result")
            return jsonify({
                'success': True,
                'enhanced': False,
                'error': 'AI analysis did not return results'
            })

    except Exception as e:
        enhance_log.error(f"Enhancement error: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/execute', methods=['POST'])
@async_route
async def execute_opportunity():
    """Execute a trade on an opportunity."""
    try:
        data = request.json
        token_id = data.get('token_id')
        amount = data.get('amount')
        side = data.get('side', 'Yes')
        tp_pct = data.get('take_profit_pct')
        sl_pct = data.get('stop_loss_pct')

        if not token_id or not amount:
            return jsonify({'success': False, 'error': 'token_id and amount required'}), 400

        # Get current price
        mid = client.get_midpoint_price(token_id)

        # Place order
        if side.lower() in ('yes', 'y'):
            result = client.buy_yes(token_id, amount)
        else:
            result = client.buy_no(token_id, amount)

        if not (result.get("success") or result.get("orderID")):
            return jsonify({'success': False, 'error': result.get('error', 'Order failed')}), 500

        # Add to PM if TP/SL specified
        pm_config = None
        if tp_pct or sl_pct:
            manager = get_manager()
            shares = amount / mid if mid > 0 else 0

            try:
                config = manager.add(
                    token_id=token_id,
                    name=f"Trade {token_id[:20]}",
                    side=side,
                    shares=shares,
                    entry_price=mid,
                    take_profit_pct=tp_pct,
                    stop_loss_pct=sl_pct,
                )
                pm_config = config.id
                _restart_monitor_if_running(manager)
            except:
                pass

        return jsonify({
            'success': True,
            'order_id': result.get('orderID'),
            'pm_config': pm_config,
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/sell', methods=['POST'])
@async_route
async def sell_position():
    """Sell a single position at market price."""
    try:
        data = request.json
        token_id = data.get('token_id')
        size = data.get('size')

        if not token_id or not size:
            return jsonify({'success': False, 'error': 'token_id and size required'}), 400

        # Get best bid price
        book = client.get_order_book(token_id)
        bids = book.bids if hasattr(book, 'bids') else book.get("bids", [])
        if not bids:
            return jsonify({'success': False, 'error': 'No bids available'}), 400

        best_bid = float(bids[0].price if hasattr(bids[0], 'price') else bids[0]['price'])
        sell_price = max(best_bid - 0.001, 0.01)

        result = client.place_order(
            token_id=token_id,
            side="sell",
            size=float(size),
            price=sell_price,
        )

        if result.get("success") or result.get("orderID"):
            # Remove from PM if exists
            pm_removed = False
            manager = get_manager()
            config = manager.get_by_token(token_id)
            if config:
                manager.delete(config.id)
                pm_removed = True
                if manager.is_monitor_running():
                    remaining = manager.list_enabled()
                    if not remaining:
                        _stop_monitor(manager)

            return jsonify({
                'success': True,
                'order_id': result.get('orderID'),
                'price': sell_price,
                'pm_removed': pm_removed
            })
        else:
            return jsonify({'success': False, 'error': result.get('error', 'Order failed')}), 500

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/sell-all', methods=['POST'])
@async_route
async def sell_all():
    """Sell all positions at market price."""
    try:
        positions = await client.get_positions()

        if not positions:
            return jsonify({'success': False, 'error': 'No positions'}), 400

        manager = get_manager()
        sold = 0
        failed = 0

        for p in positions:
            token_id = p.get('asset', '')
            size = float(p.get('size', 0))

            try:
                book = client.get_order_book(token_id)
                bids = book.bids if hasattr(book, 'bids') else book.get("bids", [])
                if not bids:
                    failed += 1
                    continue

                best_bid = float(bids[0].price if hasattr(bids[0], 'price') else bids[0]['price'])
                sell_price = max(best_bid - 0.001, 0.01)

                result = client.place_order(
                    token_id=token_id,
                    side="sell",
                    size=size,
                    price=sell_price,
                )

                if result.get("success") or result.get("orderID"):
                    sold += 1
                    config = manager.get_by_token(token_id)
                    if config:
                        manager.delete(config.id)
                else:
                    failed += 1
            except:
                failed += 1

        if sold > 0 and manager.is_monitor_running():
            remaining = manager.list_enabled()
            if not remaining:
                _stop_monitor(manager)

        return jsonify({
            'success': True,
            'sold': sold,
            'failed': failed,
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ============== Copy Trading ==============

@app.route('/api/ct/status')
def ct_status():
    """Get copy trader status."""
    try:
        ct_manager = get_ct_manager()
        pid = ct_manager.get_pid()
        configs = ct_manager.list_all()
        enabled = [c for c in configs if c.enabled]

        return jsonify({
            'success': True,
            'running': pid is not None,
            'pid': pid,
            'total_configs': len(configs),
            'enabled_configs': len(enabled),
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/ct/configs')
def ct_configs():
    """Get all copy trading configurations."""
    try:
        ct_manager = get_ct_manager()
        ct_manager.load()  # Reload from disk (daemon may have updated timestamps)
        configs = ct_manager.list_all()

        result = []
        for c in configs:
            result.append({
                'id': c.id,
                'handle': c.handle,
                'wallet_address': c.wallet_address,
                'profile_name': c.profile_name,
                'max_amount': c.max_amount,
                'extra_pct': c.extra_pct,
                'enabled': c.enabled,
                'created_at': c.created_at,
                'updated_at': c.updated_at,
                'last_check_timestamp': c.last_check_timestamp,
            })

        return jsonify({
            'success': True,
            'configs': result,
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/ct/add', methods=['POST'])
@async_route
async def ct_add():
    """Add a trader to follow. Resolves handle to wallet address."""
    try:
        import aiohttp

        data = request.json
        handle = (data.get('handle') or '').strip().lstrip('@')
        max_amount = float(data.get('max_amount', 5))
        extra_pct = float(data.get('extra_pct', 10)) / 100  # UI sends %, store as decimal

        if not handle:
            return jsonify({'success': False, 'error': 'Handle is required'}), 400

        # Resolve handle to wallet address via Gamma API
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://gamma-api.polymarket.com/public-search",
                params={"q": handle, "search_profiles": "true"},
                timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                if resp.status != 200:
                    return jsonify({'success': False, 'error': 'Failed to search Polymarket profiles'}), 500
                search_data = await resp.json()

        # Find matching profile
        profiles = search_data.get('profiles', []) if isinstance(search_data, dict) else []
        if not profiles:
            return jsonify({'success': False, 'error': f'No profile found for @{handle}'}), 404

        # Find exact or closest match
        profile = None
        for p in profiles:
            username = (p.get('username') or p.get('name') or '').lower()
            if username == handle.lower():
                profile = p
                break
        if not profile:
            profile = profiles[0]  # Use best match

        wallet_address = profile.get('proxyWallet') or profile.get('address') or ''
        profile_name = profile.get('name') or profile.get('username') or handle

        if not wallet_address:
            return jsonify({'success': False, 'error': f'Could not resolve wallet for @{handle}'}), 404

        ct_manager = get_ct_manager()

        config = ct_manager.add(
            handle=handle,
            wallet_address=wallet_address,
            profile_name=profile_name,
            max_amount=max_amount,
            extra_pct=extra_pct,
        )

        _restart_copy_trader_if_running()

        return jsonify({
            'success': True,
            'config': {
                'id': config.id,
                'handle': config.handle,
                'wallet_address': config.wallet_address,
                'profile_name': config.profile_name,
                'max_amount': config.max_amount,
                'extra_pct': config.extra_pct,
            }
        })
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/ct/config/<config_id>', methods=['PUT'])
def ct_update_config(config_id):
    """Update a copy trading config."""
    try:
        ct_manager = get_ct_manager()
        config = ct_manager.get(config_id)
        if not config:
            return jsonify({'success': False, 'error': 'Config not found'}), 404

        data = request.json
        updates = {}

        if 'max_amount' in data:
            updates['max_amount'] = float(data['max_amount'])
        if 'extra_pct' in data:
            updates['extra_pct'] = float(data['extra_pct']) / 100  # UI sends %, store as decimal
        if 'enabled' in data:
            updates['enabled'] = data['enabled']

        if updates:
            config = ct_manager.update(config_id, **updates)
            _restart_copy_trader_if_running()

        return jsonify({
            'success': True,
            'config': {
                'id': config.id,
                'handle': config.handle,
                'max_amount': config.max_amount,
                'extra_pct': config.extra_pct,
                'enabled': config.enabled,
            }
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/ct/config/<config_id>', methods=['DELETE'])
def ct_delete_config(config_id):
    """Remove a followed trader."""
    try:
        ct_manager = get_ct_manager()
        config = ct_manager.get(config_id)
        if not config:
            return jsonify({'success': False, 'error': 'Config not found'}), 404

        ct_manager.delete(config_id)
        _restart_copy_trader_if_running()

        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/ct/start', methods=['POST'])
def ct_start():
    """Start the copy trader daemon."""
    try:
        ct_manager = get_ct_manager()

        if ct_manager.is_running():
            return jsonify({'success': False, 'error': 'Already running'}), 400

        configs = ct_manager.list_enabled()
        if not configs:
            return jsonify({'success': False, 'error': 'No enabled configs'}), 400

        # Start copy trader
        import subprocess
        ct_script = os.path.join(os.path.dirname(__file__), 'copy_trader.py')
        log_file = str(CT_LOG_FILE)
        CT_CONFIG_DIR.mkdir(exist_ok=True)
        cmd = f"nohup python -u {ct_script} >> {log_file} 2>&1 &"
        subprocess.Popen(cmd, shell=True, start_new_session=True)

        import time
        time.sleep(2)

        if ct_manager.is_running():
            return jsonify({
                'success': True,
                'pid': ct_manager.get_pid()
            })
        else:
            return jsonify({'success': False, 'error': 'Failed to start'}), 500
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/ct/stop', methods=['POST'])
def ct_stop():
    """Stop the copy trader daemon."""
    try:
        ct_manager = get_ct_manager()
        pid = ct_manager.get_pid()

        if not pid:
            return jsonify({'success': False, 'error': 'Not running'}), 400

        _stop_copy_trader(ct_manager)

        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/ct/history')
def ct_history():
    """Get recent copy trader logs, parsed for terminal display."""
    try:
        import re
        lines_count = request.args.get('lines', 150, type=int)

        if not CT_LOG_FILE.exists():
            return jsonify({'success': True, 'logs': []})

        with open(CT_LOG_FILE, 'r') as f:
            all_lines = f.readlines()

        # Deduplicate consecutive lines and parse
        parsed = []
        prev = None
        # Pattern: [HH:MM:SS] [COPY_TRADING] message
        pattern = re.compile(r'^\[(\d{2}:\d{2}:\d{2})\]\s+\[COPY_TRADING\]\s*(.*)')

        for line in all_lines:
            line = line.rstrip()
            if not line or line == prev:
                continue
            prev = line

            m = pattern.match(line)
            if m:
                parsed.append({
                    'time': m.group(1),
                    'message': m.group(2),
                })

        return jsonify({
            'success': True,
            'logs': parsed[-lines_count:]
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/ct/detected-trades')
def ct_detected_trades():
    """Get detected trades from followed users."""
    try:
        if not CT_DETECTED_TRADES_FILE.exists():
            return jsonify({'success': True, 'trades': [], 'run_timestamp': None})
        with open(CT_DETECTED_TRADES_FILE, 'r') as f:
            data = json.load(f)

        # Support both old (list) and new (dict with run_timestamp) formats
        if isinstance(data, list):
            trades = data
            run_timestamp = None
        else:
            trades = data.get('trades', [])
            run_timestamp = data.get('run_timestamp')

        # Enrich with current prices
        client = PolymarketClient()
        token_ids = list({t['token_id'] for t in trades if t.get('token_id')})
        price_cache = {}
        for tid in token_ids:
            try:
                price_cache[tid] = client.get_price(tid, 'buy')
            except Exception:
                price_cache[tid] = None

        for t in trades:
            t['current_price'] = price_cache.get(t.get('token_id'))
            if t['current_price'] and t.get('size'):
                t['current_value'] = float(t['size']) * t['current_price']
            else:
                t['current_value'] = None

        # Return newest first
        trades.reverse()
        return jsonify({'success': True, 'trades': trades, 'run_timestamp': run_timestamp})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/ct/executed-trades')
def ct_executed_trades():
    """Get executed copy trades."""
    try:
        if not CT_EXECUTED_TRADES_FILE.exists():
            return jsonify({'success': True, 'trades': [], 'run_timestamp': None})
        with open(CT_EXECUTED_TRADES_FILE, 'r') as f:
            data = json.load(f)

        # Support both old (list) and new (dict with run_timestamp) formats
        if isinstance(data, list):
            trades = data
            run_timestamp = None
        else:
            trades = data.get('trades', [])
            run_timestamp = data.get('run_timestamp')

        # Enrich with current prices
        client = PolymarketClient()
        token_ids = list({t['token_id'] for t in trades if t.get('token_id')})
        price_cache = {}
        for tid in token_ids:
            try:
                price_cache[tid] = client.get_price(tid, 'buy')
            except Exception:
                price_cache[tid] = None

        for t in trades:
            t['current_price'] = price_cache.get(t.get('token_id'))
            if t['current_price'] and t.get('size'):
                t['current_value'] = float(t['size']) * t['current_price']
            else:
                t['current_value'] = None

        # Return newest first
        trades.reverse()
        return jsonify({'success': True, 'trades': trades, 'run_timestamp': run_timestamp})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ============== Helpers ==============

def _stop_copy_trader(ct_manager):
    """Stop the copy trader process."""
    pid = ct_manager.get_pid()
    if pid:
        try:
            os.kill(pid, 15)
            import time
            time.sleep(1)
            try:
                os.kill(pid, 0)
                os.kill(pid, 9)
            except ProcessLookupError:
                pass
        except:
            pass
        ct_manager.clear_pid()


def _restart_copy_trader_if_running():
    """Restart copy trader if running."""
    ct_manager = get_ct_manager()
    if ct_manager.is_running():
        _stop_copy_trader(ct_manager)
        import time
        time.sleep(1)

        configs = ct_manager.list_enabled()
        if configs:
            import subprocess
            ct_script = os.path.join(os.path.dirname(__file__), 'copy_trader.py')
            log_file = str(CT_LOG_FILE)
            CT_CONFIG_DIR.mkdir(exist_ok=True)
            cmd = f"nohup python -u {ct_script} >> {log_file} 2>&1 &"
            subprocess.Popen(cmd, shell=True, start_new_session=True)


def _stop_monitor(manager):
    """Stop the monitor process."""
    pid = manager.get_monitor_pid()
    if pid:
        try:
            os.kill(pid, 15)
            import time
            time.sleep(1)
            try:
                os.kill(pid, 0)
                os.kill(pid, 9)
            except ProcessLookupError:
                pass
        except:
            pass
        manager.clear_monitor_pid()


def _restart_monitor_if_running(manager):
    """Restart monitor if running."""
    if manager.is_monitor_running():
        _stop_monitor(manager)
        import time
        time.sleep(1)

        configs = manager.list_enabled()
        if configs:
            import subprocess
            monitor_script = os.path.join(os.path.dirname(__file__), 'profit_monitor.py')
            log_file = str(LOG_FILE)
            cmd = f"nohup python -u {monitor_script} >> {log_file} 2>&1 &"
            subprocess.Popen(cmd, shell=True, start_new_session=True)


# ============== Static Files ==============

@app.route('/')
def index():
    return send_from_directory('web_ui', 'index.html')


@app.route('/<path:path>')
def static_files(path):
    return send_from_directory('web_ui', path)


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 7070))
    debug = os.environ.get('RAILWAY_ENVIRONMENT') is None  # debug only locally
    app.run(host='0.0.0.0', port=port, debug=debug, threaded=True)
