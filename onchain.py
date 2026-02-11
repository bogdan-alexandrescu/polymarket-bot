"""Direct on-chain operations for Polymarket on Polygon."""

from web3 import Web3
from eth_account import Account
from typing import Optional
import config

# Polymarket contract addresses on Polygon
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
CTF_EXCHANGE = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
NEG_RISK_CTF_EXCHANGE = "0xC5d563A36AE78145C45a50134d48A1215220f80a"
NEG_RISK_ADAPTER = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"

# Minimal ABIs for common operations
ERC20_ABI = [
    {
        "name": "approve",
        "type": "function",
        "inputs": [
            {"name": "spender", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "outputs": [{"name": "", "type": "bool"}],
    },
    {
        "name": "allowance",
        "type": "function",
        "inputs": [
            {"name": "owner", "type": "address"},
            {"name": "spender", "type": "address"},
        ],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "balanceOf",
        "type": "function",
        "inputs": [{"name": "account", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
]


class OnchainClient:
    """Direct on-chain operations for Polymarket."""

    def __init__(self, private_key: str = None, rpc_url: str = None):
        self.private_key = private_key or config.PRIVATE_KEY
        self.rpc_url = rpc_url or config.RPC_URL
        self.w3 = Web3(Web3.HTTPProvider(self.rpc_url))

        if self.private_key:
            self.account = Account.from_key(self.private_key)
            self.address = self.account.address
        else:
            self.account = None
            self.address = None

    def get_usdc_balance(self, address: str = None) -> float:
        """Get USDC balance (human readable)."""
        addr = address or self.address
        if not addr:
            return 0.0

        usdc = self.w3.eth.contract(
            address=Web3.to_checksum_address(USDC_ADDRESS),
            abi=ERC20_ABI,
        )
        balance = usdc.functions.balanceOf(Web3.to_checksum_address(addr)).call()
        return balance / 1e6  # USDC has 6 decimals

    def get_matic_balance(self, address: str = None) -> float:
        """Get MATIC balance."""
        addr = address or self.address
        if not addr:
            return 0.0

        balance = self.w3.eth.get_balance(Web3.to_checksum_address(addr))
        return self.w3.from_wei(balance, "ether")

    def approve_usdc(
        self,
        spender: str,
        amount: float,
        max_approval: bool = True,
    ) -> str:
        """Approve USDC spending."""
        if not self.account:
            raise ValueError("No private key configured")

        usdc = self.w3.eth.contract(
            address=Web3.to_checksum_address(USDC_ADDRESS),
            abi=ERC20_ABI,
        )

        if max_approval:
            amount_raw = 2**256 - 1
        else:
            amount_raw = int(amount * 1e6)

        nonce = self.w3.eth.get_transaction_count(self.address)
        gas_price = self.w3.eth.gas_price

        tx = usdc.functions.approve(
            Web3.to_checksum_address(spender),
            amount_raw,
        ).build_transaction({
            "from": self.address,
            "nonce": nonce,
            "gasPrice": gas_price,
            "chainId": config.CHAIN_ID,
        })

        signed = self.account.sign_transaction(tx)
        tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        return tx_hash.hex()

    def approve_for_trading(self) -> dict:
        """Approve USDC for all Polymarket exchanges."""
        results = {}

        for name, addr in [
            ("CTF_EXCHANGE", CTF_EXCHANGE),
            ("NEG_RISK_CTF_EXCHANGE", NEG_RISK_CTF_EXCHANGE),
        ]:
            try:
                tx_hash = self.approve_usdc(addr, 0, max_approval=True)
                results[name] = {"status": "success", "tx_hash": tx_hash}
            except Exception as e:
                results[name] = {"status": "error", "error": str(e)}

        return results

    def check_allowance(self, spender: str) -> float:
        """Check USDC allowance."""
        if not self.address:
            return 0.0

        usdc = self.w3.eth.contract(
            address=Web3.to_checksum_address(USDC_ADDRESS),
            abi=ERC20_ABI,
        )
        allowance = usdc.functions.allowance(
            Web3.to_checksum_address(self.address),
            Web3.to_checksum_address(spender),
        ).call()
        return allowance / 1e6

    def get_gas_price(self) -> dict:
        """Get current gas prices."""
        gas_price = self.w3.eth.gas_price
        return {
            "wei": gas_price,
            "gwei": self.w3.from_wei(gas_price, "gwei"),
        }

    def wait_for_tx(self, tx_hash: str, timeout: int = 120) -> dict:
        """Wait for transaction confirmation."""
        receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=timeout)
        return {
            "status": "success" if receipt["status"] == 1 else "failed",
            "block_number": receipt["blockNumber"],
            "gas_used": receipt["gasUsed"],
            "tx_hash": tx_hash,
        }
