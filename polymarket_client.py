"""Polymarket CLOB API client for trading."""

import time
from typing import Optional, Literal
from decimal import Decimal, ROUND_DOWN
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    ApiCreds,
    OrderArgs,
    PartialCreateOrderOptions,
)
from py_clob_client.order_builder.constants import BUY, SELL
import aiohttp
import config

# Contract addresses on Polygon Mainnet
USDC_POLYGON = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
NEG_RISK_ADAPTER = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"
ZERO_BYTES32 = b'\x00' * 32

# Minimal ABIs for redemption
CTF_REDEEM_ABI = [{
    "inputs": [
        {"name": "collateralToken", "type": "address"},
        {"name": "parentCollectionId", "type": "bytes32"},
        {"name": "conditionId", "type": "bytes32"},
        {"name": "indexSets", "type": "uint256[]"},
    ],
    "name": "redeemPositions",
    "outputs": [],
    "type": "function",
}]

NEG_RISK_REDEEM_ABI = [{
    "inputs": [
        {"name": "conditionId", "type": "bytes32"},
        {"name": "amounts", "type": "uint256[]"},
    ],
    "name": "redeemPositions",
    "outputs": [],
    "type": "function",
}]

SAFE_ABI = [
    {
        "inputs": [
            {"name": "to", "type": "address"},
            {"name": "value", "type": "uint256"},
            {"name": "data", "type": "bytes"},
            {"name": "operation", "type": "uint8"},
            {"name": "safeTxGas", "type": "uint256"},
            {"name": "baseGas", "type": "uint256"},
            {"name": "gasPrice", "type": "uint256"},
            {"name": "gasToken", "type": "address"},
            {"name": "refundReceiver", "type": "address"},
            {"name": "signatures", "type": "bytes"},
        ],
        "name": "execTransaction",
        "outputs": [{"name": "success", "type": "bool"}],
        "type": "function",
    },
    {
        "inputs": [
            {"name": "to", "type": "address"},
            {"name": "value", "type": "uint256"},
            {"name": "data", "type": "bytes"},
            {"name": "operation", "type": "uint8"},
            {"name": "safeTxGas", "type": "uint256"},
            {"name": "baseGas", "type": "uint256"},
            {"name": "gasPrice", "type": "uint256"},
            {"name": "gasToken", "type": "address"},
            {"name": "refundReceiver", "type": "address"},
            {"name": "_nonce", "type": "uint256"},
        ],
        "name": "getTransactionHash",
        "outputs": [{"name": "", "type": "bytes32"}],
        "type": "function",
    },
    {
        "inputs": [],
        "name": "nonce",
        "outputs": [{"name": "", "type": "uint256"}],
        "type": "function",
    },
]


class PolymarketClient:
    """Efficient client for Polymarket CLOB trading."""

    def __init__(
        self,
        private_key: str = None,
        api_key: str = None,
        api_secret: str = None,
        api_passphrase: str = None,
        proxy_wallet: str = None,
        signature_type: int = None,
    ):
        self.private_key = private_key or config.PRIVATE_KEY
        self.proxy_wallet = proxy_wallet or config.PROXY_WALLET
        self.signature_type = signature_type if signature_type is not None else config.SIGNATURE_TYPE
        self.api_creds = None

        if api_key or config.API_KEY:
            self.api_creds = ApiCreds(
                api_key=api_key or config.API_KEY,
                api_secret=api_secret or config.API_SECRET,
                api_passphrase=api_passphrase or config.API_PASSPHRASE,
            )

        # Configure client with proxy wallet if set
        self.client = ClobClient(
            host=config.CLOB_API_URL,
            chain_id=config.CHAIN_ID,
            key=self.private_key,
            creds=self.api_creds,
            signature_type=self.signature_type if self.proxy_wallet else None,
            funder=self.proxy_wallet,
        )
        self._address = None

    @property
    def address(self) -> str:
        if not self._address and self.private_key:
            from eth_account import Account
            self._address = Account.from_key(self.private_key).address
        return self._address

    def derive_api_key(self) -> ApiCreds:
        """Derive API credentials from private key (one-time setup)."""
        creds = self.client.derive_api_key()
        print(f"API Key: {creds.api_key}")
        print(f"API Secret: {creds.api_secret}")
        print(f"API Passphrase: {creds.api_passphrase}")
        return creds

    def create_api_key(self) -> ApiCreds:
        """Create new API key (requires existing creds or signature)."""
        creds = self.client.create_api_key()
        return creds

    # ==================== MARKET DATA ====================

    async def get_markets(
        self,
        limit: int = 100,
        offset: int = 0,
        active: bool = True,
    ) -> list[dict]:
        """Fetch markets from Gamma API."""
        params = {
            "limit": limit,
            "offset": offset,
            "active": str(active).lower(),
        }
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{config.GAMMA_API_URL}/markets",
                params=params,
            ) as resp:
                return await resp.json()

    async def get_market(self, condition_id: str) -> dict:
        """Get single market by condition ID."""
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{config.GAMMA_API_URL}/markets/{condition_id}"
            ) as resp:
                return await resp.json()

    async def search_markets(self, query: str, limit: int = 50) -> list[dict]:
        """Search markets by query string."""
        import re

        async with aiohttp.ClientSession() as session:
            # Fetch events ordered by volume (most popular first)
            async with session.get(
                f"{config.GAMMA_API_URL}/events",
                params={
                    "closed": "false",
                    "limit": 1000,
                    "order": "volume",
                    "ascending": "false",
                },
            ) as resp:
                events = await resp.json()

            # Normalize query - remove punctuation, lowercase
            def normalize(text: str) -> str:
                return re.sub(r'[^\w\s]', '', text.lower())

            query_normalized = normalize(query)
            query_words = query_normalized.split()

            results = []
            for event in events:
                # Check multiple fields
                title = normalize(event.get("title", ""))
                slug = normalize(event.get("slug", "").replace("-", " "))
                ticker = normalize(event.get("ticker", "").replace("-", " "))
                desc = normalize(event.get("description", "")[:200])

                searchable = f"{title} {slug} {ticker} {desc}"

                # Match if all query words appear in searchable text
                if all(word in searchable for word in query_words):
                    for market in event.get("markets", []):
                        market["_event"] = event.get("title")
                        results.append(market)

            # Filter out closed markets and sort by end date (upcoming first)
            active_results = []
            for r in results:
                if r.get("closed", False):
                    continue
                # Parse outcomePrices to check if resolved (both 0 or one is 100%)
                prices = r.get("outcomePrices", "[]")
                try:
                    import json
                    prices = json.loads(prices) if isinstance(prices, str) else prices
                    yes_price = float(prices[0]) if prices else 0
                    # Skip if YES is 0% or 100% (resolved)
                    if yes_price <= 0.01 or yes_price >= 0.99:
                        continue
                except:
                    pass
                active_results.append(r)

            active_results.sort(key=lambda x: x.get("endDate", "9999"))

            return active_results[:limit]

    def get_order_book(self, token_id: str):
        """Get order book for a token (YES or NO outcome)."""
        return self.client.get_order_book(token_id)

    def get_price(self, token_id: str, side: Literal["buy", "sell"] = "buy") -> float:
        """Get best price for a token."""
        book = self.get_order_book(token_id)
        if side == "buy" and book.asks:
            return float(book.asks[0].price)
        elif side == "sell" and book.bids:
            return float(book.bids[0].price)
        return 0.0

    def get_midpoint_price(self, token_id: str) -> float:
        """Get midpoint price for a token."""
        resp = self.client.get_midpoint(token_id)
        if hasattr(resp, 'mid'):
            return float(resp.mid)
        return float(resp.get("mid", 0) if isinstance(resp, dict) else 0)

    def get_spread(self, token_id: str) -> dict:
        """Get bid-ask spread for a token."""
        resp = self.client.get_spread(token_id)
        if hasattr(resp, 'bid'):
            return {
                "bid": float(resp.bid) if resp.bid else 0,
                "ask": float(resp.ask) if resp.ask else 0,
                "spread": float(resp.spread) if resp.spread else 0,
            }
        return {
            "bid": float(resp.get("bid", 0)),
            "ask": float(resp.get("ask", 0)),
            "spread": float(resp.get("spread", 0)),
        }

    # ==================== TRADING ====================

    def _round_price(self, price: float) -> float:
        """Round price to valid tick size."""
        d = Decimal(str(price))
        tick = Decimal(str(config.MIN_TICK_SIZE))
        return float(d.quantize(tick, rounding=ROUND_DOWN))

    def _round_size(self, size: float) -> float:
        """Round size to 2 decimals."""
        return float(Decimal(str(size)).quantize(Decimal("0.01"), rounding=ROUND_DOWN))

    def place_order(
        self,
        token_id: str,
        side: Literal["buy", "sell"],
        size: float,
        price: float,
    ) -> dict:
        """
        Place a limit order (GTC - Good Till Cancel).

        Args:
            token_id: The token ID (YES or NO token)
            side: "buy" or "sell"
            size: Amount in outcome tokens
            price: Price per token (0.01 to 0.99)
        """
        price = self._round_price(price)
        size = self._round_size(size)

        order_args = OrderArgs(
            token_id=token_id,
            price=price,
            size=size,
            side=BUY if side == "buy" else SELL,
        )

        signed_order = self.client.create_order(order_args)
        result = self.client.post_order(signed_order)
        return result

    def place_market_order(
        self,
        token_id: str,
        side: Literal["buy", "sell"],
        amount: float,
    ) -> dict:
        """
        Place a market order (limit order at best available price + slippage).

        Args:
            token_id: The token ID
            side: "buy" or "sell"
            amount: Dollar amount to spend/receive
        """
        price = self.get_price(token_id, side)
        if price == 0:
            raise ValueError("No liquidity available")

        # Add slippage for buys, subtract for sells
        if side == "buy":
            price = min(0.99, price + config.DEFAULT_SLIPPAGE)
            size = amount / price
        else:
            price = max(0.01, price - config.DEFAULT_SLIPPAGE)
            size = amount

        return self.place_order(
            token_id=token_id,
            side=side,
            size=size,
            price=price,
        )

    def buy_yes(self, token_id: str, amount: float, price: float = None) -> dict:
        """Buy YES tokens."""
        if price:
            size = amount / price
            return self.place_order(token_id, "buy", size, price)
        return self.place_market_order(token_id, "buy", amount)

    def buy_no(self, token_id: str, amount: float, price: float = None) -> dict:
        """Buy NO tokens (token_id should be the NO token)."""
        if price:
            size = amount / price
            return self.place_order(token_id, "buy", size, price)
        return self.place_market_order(token_id, "buy", amount)

    def sell_yes(self, token_id: str, size: float, price: float = None) -> dict:
        """Sell YES tokens."""
        if price:
            return self.place_order(token_id, "sell", size, price)
        return self.place_market_order(token_id, "sell", size)

    def sell_no(self, token_id: str, size: float, price: float = None) -> dict:
        """Sell NO tokens."""
        if price:
            return self.place_order(token_id, "sell", size, price)
        return self.place_market_order(token_id, "sell", size)

    # ==================== ORDER MANAGEMENT ====================

    def get_orders(self, market: str = None) -> list[dict]:
        """Get open orders."""
        params = {"market": market} if market else {}
        return self.client.get_orders(params)

    def get_order(self, order_id: str) -> dict:
        """Get specific order by ID."""
        return self.client.get_order(order_id)

    def cancel_order(self, order_id: str) -> dict:
        """Cancel a specific order."""
        return self.client.cancel(order_id)

    def cancel_all_orders(self) -> dict:
        """Cancel all open orders."""
        return self.client.cancel_all()

    def cancel_market_orders(self, market: str) -> dict:
        """Cancel all orders for a specific market."""
        return self.client.cancel_market_orders(market)

    # ==================== POSITIONS & BALANCE ====================

    async def get_positions(self) -> list[dict]:
        """Get all positions for the account."""
        # Use proxy wallet if configured, otherwise EOA
        wallet = self.proxy_wallet or self.address
        if not wallet:
            return []
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"https://data-api.polymarket.com/positions",
                params={"user": wallet},
            ) as resp:
                if resp.status == 200:
                    return await resp.json()
                return []

    async def get_balance(self) -> dict:
        """Get USDC balance on Polymarket."""
        if not self.address:
            return {"balance": 0}
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{config.CLOB_API_URL}/balance",
                params={"address": self.address},
            ) as resp:
                return await resp.json()

    # ==================== REDEMPTION ====================

    def _get_w3(self):
        """Get web3 instance connected to Polygon."""
        from web3 import Web3
        w3 = Web3(Web3.HTTPProvider(config.RPC_URL))
        if not w3.is_connected():
            raise ConnectionError(f"Cannot connect to Polygon RPC: {config.RPC_URL}")
        return w3

    def redeem_position(self, condition_id: str, negative_risk: bool = False) -> dict:
        """
        Redeem a resolved position via the CTF contract.

        For proxy/Safe wallets, executes through the Gnosis Safe.
        For EOA wallets, calls CTF directly.

        Returns dict with 'success', 'tx_hash', and 'error' keys.
        """
        from web3 import Web3
        from eth_account import Account
        from eth_account.messages import encode_defunct

        w3 = self._get_w3()
        account = Account.from_key(self.private_key)
        eoa_address = account.address

        # Encode the CTF redeemPositions call data
        condition_id_bytes = Web3.to_bytes(hexstr=condition_id)

        if negative_risk:
            contract = w3.eth.contract(
                address=Web3.to_checksum_address(NEG_RISK_ADAPTER),
                abi=NEG_RISK_REDEEM_ABI,
            )
            # For neg risk, pass large amounts for each outcome to redeem everything
            max_uint = 2**256 - 1
            call_data = contract.functions.redeemPositions(
                condition_id_bytes,
                [max_uint, max_uint],
            )._encode_transaction_data()
            target = Web3.to_checksum_address(NEG_RISK_ADAPTER)
        else:
            contract = w3.eth.contract(
                address=Web3.to_checksum_address(CTF_ADDRESS),
                abi=CTF_REDEEM_ABI,
            )
            call_data = contract.functions.redeemPositions(
                Web3.to_checksum_address(USDC_POLYGON),
                ZERO_BYTES32,
                condition_id_bytes,
                [1, 2],
            )._encode_transaction_data()
            target = Web3.to_checksum_address(CTF_ADDRESS)

        call_data_bytes = Web3.to_bytes(hexstr=call_data) if isinstance(call_data, str) else call_data

        # Route through Safe if using proxy wallet, otherwise send directly
        if self.proxy_wallet:
            return self._redeem_via_safe(w3, account, eoa_address, target, call_data_bytes)
        else:
            return self._redeem_direct(w3, account, eoa_address, target, call_data_bytes)

    def _redeem_direct(self, w3, account, eoa_address, target, call_data) -> dict:
        """Send redemption tx directly from EOA."""
        try:
            tx = {
                'to': target,
                'data': call_data,
                'value': 0,
                'from': eoa_address,
                'nonce': w3.eth.get_transaction_count(eoa_address),
                'gas': 300000,
                'maxFeePerGas': w3.eth.gas_price * 2,
                'maxPriorityFeePerGas': w3.to_wei(30, 'gwei'),
                'chainId': config.CHAIN_ID,
            }
            signed = account.sign_transaction(tx)
            tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
            return {
                'success': receipt.status == 1,
                'tx_hash': tx_hash.hex(),
                'error': None if receipt.status == 1 else 'Transaction reverted',
            }
        except Exception as e:
            return {'success': False, 'tx_hash': None, 'error': str(e)}

    def _redeem_via_safe(self, w3, account, eoa_address, target, call_data) -> dict:
        """Execute redemption through Gnosis Safe."""
        from web3 import Web3

        try:
            safe_address = Web3.to_checksum_address(self.proxy_wallet)
            safe = w3.eth.contract(address=safe_address, abi=SAFE_ABI)

            # Safe tx parameters (zero gas params = EOA pays gas directly)
            zero_addr = Web3.to_checksum_address('0x' + '0' * 40)
            safe_nonce = safe.functions.nonce().call()

            # Get the Safe transaction hash for signing
            safe_tx_hash = safe.functions.getTransactionHash(
                target,          # to
                0,               # value
                call_data,       # data
                0,               # operation (CALL)
                0,               # safeTxGas
                0,               # baseGas
                0,               # gasPrice
                zero_addr,       # gasToken
                zero_addr,       # refundReceiver
                safe_nonce,      # _nonce
            ).call()

            # Sign the hash
            signature = account.unsafe_sign_hash(safe_tx_hash)

            # Encode signature: r (32 bytes) + s (32 bytes) + v (1 byte)
            sig_bytes = (
                signature.r.to_bytes(32, 'big') +
                signature.s.to_bytes(32, 'big') +
                signature.v.to_bytes(1, 'big')
            )

            # Build and send the execTransaction call
            tx = safe.functions.execTransaction(
                target,
                0,
                call_data,
                0,               # CALL operation
                0,               # safeTxGas
                0,               # baseGas
                0,               # gasPrice
                zero_addr,       # gasToken
                zero_addr,       # refundReceiver
                sig_bytes,
            ).build_transaction({
                'from': eoa_address,
                'nonce': w3.eth.get_transaction_count(eoa_address),
                'gas': 500000,
                'maxFeePerGas': w3.eth.gas_price * 2,
                'maxPriorityFeePerGas': w3.to_wei(30, 'gwei'),
                'chainId': config.CHAIN_ID,
            })

            signed = account.sign_transaction(tx)
            tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

            return {
                'success': receipt.status == 1,
                'tx_hash': tx_hash.hex(),
                'error': None if receipt.status == 1 else 'Transaction reverted',
            }
        except Exception as e:
            return {'success': False, 'tx_hash': None, 'error': str(e)}

    # ==================== TRADE HISTORY ====================

    async def get_trades(
        self,
        market: str = None,
        limit: int = 100,
    ) -> list[dict]:
        """Get trade history."""
        params = {"limit": limit}
        if market:
            params["market"] = market
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{config.CLOB_API_URL}/trades",
                params=params,
            ) as resp:
                return await resp.json()
