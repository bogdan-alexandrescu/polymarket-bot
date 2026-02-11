"""Configuration for Polymarket trading bot."""

import os
from dotenv import load_dotenv

load_dotenv()

# Polymarket CLOB API
CLOB_API_URL = "https://clob.polymarket.com"
GAMMA_API_URL = "https://gamma-api.polymarket.com"

# Chain configuration (Polygon)
CHAIN_ID = 137
RPC_URL = os.getenv("POLYGON_RPC_URL", "https://polygon-rpc.com")

# Authentication
PRIVATE_KEY = os.getenv("POLYMARKET_PRIVATE_KEY")
API_KEY = os.getenv("POLYMARKET_API_KEY")
API_SECRET = os.getenv("POLYMARKET_API_SECRET")
API_PASSPHRASE = os.getenv("POLYMARKET_API_PASSPHRASE")

# Twilio SMS configuration
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER")
ALERT_PHONE_NUMBER = os.getenv("ALERT_PHONE_NUMBER")

# Proxy wallet (Polymarket smart contract wallet)
PROXY_WALLET = os.getenv("POLYMARKET_PROXY_WALLET")

# Signature types: 0=EOA, 1=POLY_PROXY, 2=POLY_GNOSIS_SAFE
SIGNATURE_TYPE = int(os.getenv("POLYMARKET_SIGNATURE_TYPE", "2" if PROXY_WALLET else "0"))

# Trading defaults
DEFAULT_SLIPPAGE = 0.01  # 1%
MIN_TICK_SIZE = 0.01
