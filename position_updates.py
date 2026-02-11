#!/usr/bin/env python3
import time
import sys
from polymarket_client import PolymarketClient

client = PolymarketClient()
TOKEN_ID = '33500624603717915548751257601790831230034214276562194133717878870648873631267'
SHARES = 10.19
ENTRY_PRICE = 0.98
TARGET_VALUE = 10.10
TARGET_PRICE = TARGET_VALUE / SHARES

while True:
    try:
        mid = client.get_midpoint_price(TOKEN_ID)
        current_value = mid * SHARES
        pnl = current_value - (ENTRY_PRICE * SHARES)
        pnl_pct = (pnl / (ENTRY_PRICE * SHARES)) * 100
        
        print(f'\n📊 JAN 25 NO UPDATE - {time.strftime("%H:%M:%S")}')
        print(f'Price: {mid*100:.2f}% | Value: ${current_value:.2f} | P&L: {"+" if pnl >= 0 else ""}${pnl:.2f} ({"+" if pnl_pct >= 0 else ""}{pnl_pct:.1f}%)')
        print(f'Target: ${TARGET_VALUE} ({TARGET_PRICE*100:.2f}%) | Gap: ${TARGET_VALUE - current_value:.2f}')
        sys.stdout.flush()
        
        if mid >= TARGET_PRICE:
            print('🎯 TARGET REACHED!')
            break
            
    except Exception as e:
        print(f'Error: {e}')
    
    time.sleep(300)  # 5 minutes
