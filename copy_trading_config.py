#!/usr/bin/env python3
"""
Copy Trading Configuration Manager

Handles persistent storage of copy trading configurations.
Stores configs in a local JSON file for the copy trader daemon to use.
"""

import json
import os
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Optional
from pathlib import Path

# Config file location — use volume mount on Railway, local dir otherwise
_BASE_DIR = Path(os.environ.get('RAILWAY_VOLUME_MOUNT_PATH', str(Path(__file__).parent)))
CT_CONFIG_DIR = _BASE_DIR / ".copy_trading"
CT_CONFIG_FILE = CT_CONFIG_DIR / "configs.json"
CT_PID_FILE = CT_CONFIG_DIR / "copy_trader.pid"
CT_LOG_FILE = CT_CONFIG_DIR / "copy_trader.log"
CT_DETECTED_TRADES_FILE = CT_CONFIG_DIR / "detected_trades.json"
CT_EXECUTED_TRADES_FILE = CT_CONFIG_DIR / "executed_trades.json"


@dataclass
class CopyTraderConfig:
    """Configuration for a followed trader."""
    id: str  # Unique config ID (6 chars)
    handle: str  # Polymarket username (e.g., planktonXD)
    wallet_address: str  # Resolved wallet address
    profile_name: str  # Display name from profile

    # Sizing configuration
    max_amount: float = 5.0  # Max copy amount ($). Trades under this are copied at exact size.
    extra_pct: float = 0.10  # Extra % of original added when trade exceeds max (e.g. 0.10 = 10%)

    # Legacy fields (kept for backwards compat with existing configs)
    sizing_mode: str = ""
    fixed_amount: float = 0.0
    percentage: float = 0.0

    # Status
    enabled: bool = True
    created_at: str = ""
    updated_at: str = ""

    # Tracking
    last_check_timestamp: Optional[float] = None  # Unix timestamp of last activity check

    def __post_init__(self):
        if not self.created_at:
            self.created_at = datetime.now().isoformat()
        self.updated_at = datetime.now().isoformat()

    def summary(self) -> str:
        """Return a summary string."""
        status = "ON" if self.enabled else "OFF"
        return f"[{self.id}] @{self.handle} ({self.profile_name}) | max ${self.max_amount:.0f} +{self.extra_pct*100:.0f}% | {status}"


class CopyTradingConfigManager:
    """Manages copy trading configurations stored in a local file."""

    def __init__(self):
        self._ensure_config_dir()
        self.configs: dict[str, CopyTraderConfig] = {}
        self.load()

    def _ensure_config_dir(self):
        """Ensure config directory exists."""
        CT_CONFIG_DIR.mkdir(exist_ok=True)

    def _generate_id(self) -> str:
        """Generate a short unique ID."""
        import random
        import string
        return ''.join(random.choices(string.ascii_lowercase + string.digits, k=6))

    def load(self) -> dict[str, CopyTraderConfig]:
        """Load configurations from file."""
        if not CT_CONFIG_FILE.exists():
            self.configs = {}
            return self.configs

        try:
            with open(CT_CONFIG_FILE, 'r') as f:
                data = json.load(f)

            self.configs = {}
            for config_id, config_data in data.items():
                self.configs[config_id] = CopyTraderConfig(**config_data)
        except Exception as e:
            print(f"Error loading copy trading config: {e}")
            self.configs = {}

        return self.configs

    def save(self):
        """Save configurations to file."""
        self._ensure_config_dir()

        data = {config_id: asdict(config) for config_id, config in self.configs.items()}

        with open(CT_CONFIG_FILE, 'w') as f:
            json.dump(data, f, indent=2)

    def add(self,
            handle: str,
            wallet_address: str,
            profile_name: str,
            max_amount: float = 5.0,
            extra_pct: float = 0.10,
            **kwargs) -> CopyTraderConfig:
        """Add a new copy trader config."""
        config_id = self._generate_id()

        # Check if already following this wallet
        for existing in self.configs.values():
            if existing.wallet_address.lower() == wallet_address.lower():
                raise ValueError(f"Already following this trader: {existing.handle} ({existing.id})")

        config = CopyTraderConfig(
            id=config_id,
            handle=handle,
            wallet_address=wallet_address,
            profile_name=profile_name,
            max_amount=max_amount,
            extra_pct=extra_pct,
        )

        self.configs[config_id] = config
        self.save()
        return config

    def update(self, config_id: str, **kwargs) -> CopyTraderConfig:
        """Update an existing config."""
        if config_id not in self.configs:
            raise ValueError(f"Config not found: {config_id}")

        config = self.configs[config_id]

        for key, value in kwargs.items():
            if hasattr(config, key):
                setattr(config, key, value)

        config.updated_at = datetime.now().isoformat()
        self.save()
        return config

    def delete(self, config_id: str) -> bool:
        """Delete a config."""
        if config_id not in self.configs:
            return False

        del self.configs[config_id]
        self.save()
        return True

    def get(self, config_id: str) -> Optional[CopyTraderConfig]:
        """Get a config by ID."""
        return self.configs.get(config_id)

    def list_all(self) -> list[CopyTraderConfig]:
        """List all configs."""
        return list(self.configs.values())

    def list_enabled(self) -> list[CopyTraderConfig]:
        """List only enabled configs."""
        return [c for c in self.configs.values() if c.enabled]

    # PID file management for copy trader process
    def get_pid(self) -> Optional[int]:
        """Get the PID of the running copy trader, if any."""
        if not CT_PID_FILE.exists():
            return None

        try:
            pid = int(CT_PID_FILE.read_text().strip())
            # Check if process is actually running
            os.kill(pid, 0)
            return pid
        except (ValueError, ProcessLookupError, PermissionError):
            # Process not running, clean up stale PID file
            CT_PID_FILE.unlink(missing_ok=True)
            return None

    def set_pid(self, pid: int):
        """Set the copy trader PID."""
        self._ensure_config_dir()
        CT_PID_FILE.write_text(str(pid))

    def clear_pid(self):
        """Clear the copy trader PID file."""
        CT_PID_FILE.unlink(missing_ok=True)

    def is_running(self) -> bool:
        """Check if copy trader is running."""
        return self.get_pid() is not None


# Singleton instance
_ct_manager = None

def get_ct_manager() -> CopyTradingConfigManager:
    """Get the singleton copy trading config manager."""
    global _ct_manager
    if _ct_manager is None:
        _ct_manager = CopyTradingConfigManager()
    return _ct_manager
