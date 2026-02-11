#!/usr/bin/env python3
"""Monitor and auto-sell Jan 25 NO position when target price is reached."""

import asyncio
import time
from polymarket_client import PolymarketClient
from sms_alerts import SMSAlerter

# Configuration
TOKEN_ID = '33500624603717915548751257601790831230034214276562194133717878870648873631267'
SHARES = 10.19
TARGET_VALUE = 10.10
TARGET_PRICE = TARGET_VALUE / SHARES  # ~0.991
POLL_INTERVAL = 10  # seconds

client = PolymarketClient()
alerter = SMSAlerter()


async def monitor():
    print(f"{'='*50}")
    print("SELL MONITOR - US strikes Iran by Jan 25 (NO)")
    print(f"{'='*50}")
    print(f"Shares: {SHARES}")
    print(f"Target value: ${TARGET_VALUE}")
    print(f"Target price: {TARGET_PRICE*100:.2f}%")
    print(f"Poll interval: {POLL_INTERVAL}s")
    print(f"{'='*50}")
    print()

    while True:
        try:
            mid = client.get_midpoint_price(TOKEN_ID)
            current_value = mid * SHARES
            timestamp = time.strftime("%H:%M:%S")

            print(f"[{timestamp}] Price: {mid*100:.2f}% | Value: ${current_value:.2f} | Target: ${TARGET_VALUE}")

            if mid >= TARGET_PRICE:
                print()
                print("🎯 TARGET REACHED! Placing sell order...")

                result = client.place_order(TOKEN_ID, 'sell', SHARES, TARGET_PRICE)

                if result.get('success'):
                    print(f"✅ SOLD!")
                    print(f"   Order ID: {result.get('orderID')}")
                    print(f"   Amount: ${result.get('makingAmount')}")

                    alerter.send_alert(
                        f"✅ SOLD Jan 25 NO position\n"
                        f"Shares: {SHARES}\n"
                        f"Price: {mid*100:.2f}%\n"
                        f"Value: ${current_value:.2f}"
                    )
                    break
                else:
                    print(f"❌ Order failed: {result}")

        except Exception as e:
            print(f"[ERROR] {e}")

        await asyncio.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    try:
        asyncio.run(monitor())
    except KeyboardInterrupt:
        print("\nMonitor stopped.")
