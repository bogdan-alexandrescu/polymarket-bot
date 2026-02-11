# Polymarket Trading Bot

Efficient Python bot for trading on Polymarket prediction markets with SMS alerts.

## Features

- **Trading**: Buy/sell YES/NO positions with limit or market orders
- **Monitoring**: Real-time price tracking with configurable polling
- **Alerts**: SMS notifications via Twilio when prices move
- **Auto-trading**: Execute trades automatically when price thresholds are crossed
- **On-chain**: Direct Polygon blockchain operations (approvals, balances)

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env with your credentials
```

Required credentials:
- `POLYMARKET_PRIVATE_KEY`: Your Polygon wallet private key
- Twilio credentials for SMS alerts (optional)

### 3. Generate API key (first time only)

```bash
python main.py derive-key
```

Add the generated credentials to your `.env` file.

## Usage

### Command Line

```bash
# Search for markets
python main.py search "bitcoin"

# Get current price
python main.py price <token_id>

# View order book
python main.py book <token_id>

# Place orders
python main.py buy <token_id> 50         # Market buy $50
python main.py buy <token_id> 50 --price 0.45  # Limit buy at 45%
python main.py sell <token_id> 100       # Market sell 100 tokens
python main.py sell <token_id> 100 --price 0.60  # Limit sell at 60%

# View positions and orders
python main.py positions
python main.py orders

# Cancel orders
python main.py cancel <order_id>
python main.py cancel all
```

### Interactive Monitor

```bash
python main.py monitor
```

Commands:
- `add <condition_id>` - Add market to monitor
- `alert <condition_id> YES 0.05` - Alert on 5% YES price change
- `auto <condition_id> YES 0.30 below buy 50` - Buy $50 YES when price drops below 30%
- `start` - Start monitoring
- `stop` - Stop monitoring
- `status` - Show current status
- `quit` - Exit

### Config File Monitor

```bash
python main.py monitor --config monitor_config.json
```

See `monitor_config.example.json` for format.

### Python API

```python
import asyncio
from polymarket_client import PolymarketClient
from monitor import MarketMonitor, TriggerDirection
from sms_alerts import SMSAlerter

# Initialize client
client = PolymarketClient()

# Search markets
markets = asyncio.run(client.search_markets("bitcoin"))

# Get price
price = client.get_midpoint_price(token_id)

# Place orders
client.buy_yes(token_id, amount=50)  # Market buy
client.buy_yes(token_id, amount=50, price=0.45)  # Limit buy
client.sell_yes(token_id, size=100, price=0.60)  # Limit sell

# Set up monitoring with alerts
alerter = SMSAlerter()
monitor = MarketMonitor(client, alerter)

monitor.add_market(condition_id, "Market Name", yes_token_id, no_token_id)
monitor.add_price_alert(condition_id, "YES", threshold=0.05)
monitor.add_auto_trade(
    condition_id, "YES",
    trigger_price=0.30,
    direction="below",
    action="buy",
    amount=50
)

asyncio.run(monitor.run())
```

## Architecture

```
polymarket_bot/
├── config.py           # Configuration and env vars
├── polymarket_client.py # CLOB API client
├── monitor.py          # Price monitoring and auto-trading
├── sms_alerts.py       # Twilio SMS integration
├── onchain.py          # Direct Polygon operations
└── main.py             # CLI entry point
```

## Token IDs

Each market has two tokens:
- **YES token**: Pays $1 if outcome is YES, $0 otherwise
- **NO token**: Pays $1 if outcome is NO, $0 otherwise

Find token IDs using:
```bash
python main.py search "your market query"
```

## Notes

- Prices are in probability (0.01 = 1%, 0.99 = 99%)
- Minimum tick size is 0.01 (1%)
- Trading requires USDC on Polygon
- API rate limits apply - default poll interval is 5 seconds
