#!/usr/bin/env python3
"""
Monitor Configuration Manager

Handles persistent storage of take-profit (TP) and stop-loss (SL) configurations.
Stores configs in a local JSON file for the profit monitor to use.
"""

import json
import os
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Optional
from pathlib import Path

# Config file location — use volume mount on Railway, local dir otherwise
_BASE_DIR = Path(os.environ.get('RAILWAY_VOLUME_MOUNT_PATH', str(Path(__file__).parent)))
CONFIG_DIR = _BASE_DIR / ".monitor"
CONFIG_FILE = CONFIG_DIR / "positions.json"
PID_FILE = CONFIG_DIR / "monitor.pid"
LOG_FILE = CONFIG_DIR / "monitor.log"


@dataclass
class PositionConfig:
    """Configuration for a monitored position."""
    id: str  # Unique config ID
    token_id: str
    name: str
    side: str  # "Yes" or "No"
    shares: float
    entry_price: float

    # Market info
    description: str = ""  # Market description
    slug: str = ""  # URL slug for Polymarket link

    # Take profit config (optional)
    take_profit_pct: Optional[float] = None  # e.g., 0.03 for 3%
    take_profit_price: Optional[float] = None  # Absolute price

    # Stop loss config (optional)
    stop_loss_pct: Optional[float] = None  # e.g., 0.05 for 5%
    stop_loss_price: Optional[float] = None  # Absolute price

    # Status
    enabled: bool = True
    created_at: str = ""
    updated_at: str = ""

    def __post_init__(self):
        if not self.created_at:
            self.created_at = datetime.now().isoformat()
        self.updated_at = datetime.now().isoformat()

    def get_tp_target(self) -> Optional[float]:
        """Get take profit target price."""
        if self.take_profit_price:
            return self.take_profit_price
        if self.take_profit_pct:
            return self.entry_price * (1 + self.take_profit_pct)
        return None

    def get_sl_target(self) -> Optional[float]:
        """Get stop loss target price."""
        if self.stop_loss_price:
            return self.stop_loss_price
        if self.stop_loss_pct:
            return self.entry_price * (1 - self.stop_loss_pct)
        return None

    def summary(self) -> str:
        """Return a summary string."""
        tp = self.get_tp_target()
        sl = self.get_sl_target()

        tp_str = f"TP: {tp*100:.1f}%" if tp else "TP: -"
        sl_str = f"SL: {sl*100:.1f}%" if sl else "SL: -"
        status = "ON" if self.enabled else "OFF"

        return f"[{self.id}] {self.name} ({self.side}) | Entry: {self.entry_price*100:.1f}% | {tp_str} | {sl_str} | {status}"


class MonitorConfigManager:
    """Manages monitor configurations stored in a local file."""

    def __init__(self):
        self._ensure_config_dir()
        self.configs: dict[str, PositionConfig] = {}
        self.load()

    def _ensure_config_dir(self):
        """Ensure config directory exists."""
        CONFIG_DIR.mkdir(exist_ok=True)

    def _generate_id(self) -> str:
        """Generate a short unique ID."""
        import random
        import string
        return ''.join(random.choices(string.ascii_lowercase + string.digits, k=6))

    def load(self) -> dict[str, PositionConfig]:
        """Load configurations from file."""
        if not CONFIG_FILE.exists():
            self.configs = {}
            return self.configs

        try:
            with open(CONFIG_FILE, 'r') as f:
                data = json.load(f)

            self.configs = {}
            for config_id, config_data in data.items():
                self.configs[config_id] = PositionConfig(**config_data)
        except Exception as e:
            print(f"Error loading config: {e}")
            self.configs = {}

        return self.configs

    def save(self):
        """Save configurations to file."""
        self._ensure_config_dir()

        data = {config_id: asdict(config) for config_id, config in self.configs.items()}

        with open(CONFIG_FILE, 'w') as f:
            json.dump(data, f, indent=2)

    def add(self,
            token_id: str,
            name: str,
            side: str,
            shares: float,
            entry_price: float,
            take_profit_pct: Optional[float] = None,
            take_profit_price: Optional[float] = None,
            stop_loss_pct: Optional[float] = None,
            stop_loss_price: Optional[float] = None,
            description: str = "",
            slug: str = "") -> PositionConfig:
        """Add a new position config.

        TP/SL can be specified as either percentage or absolute price.
        Percentages are converted to prices and stored as prices.
        """
        config_id = self._generate_id()

        # Check if already exists for this token
        for existing in self.configs.values():
            if existing.token_id == token_id:
                raise ValueError(f"Config already exists for this token: {existing.id}")

        # Convert percentages to prices (store only prices)
        final_tp_price = take_profit_price
        if take_profit_pct is not None and final_tp_price is None:
            final_tp_price = entry_price * (1 + take_profit_pct)

        final_sl_price = stop_loss_price
        if stop_loss_pct is not None and final_sl_price is None:
            final_sl_price = entry_price * (1 - stop_loss_pct)

        config = PositionConfig(
            id=config_id,
            token_id=token_id,
            name=name,
            side=side,
            shares=shares,
            entry_price=entry_price,
            description=description,
            slug=slug,
            take_profit_pct=None,  # Don't store percentage
            take_profit_price=final_tp_price,
            stop_loss_pct=None,  # Don't store percentage
            stop_loss_price=final_sl_price,
        )

        self.configs[config_id] = config
        self.save()
        return config

    def update(self, config_id: str, **kwargs) -> PositionConfig:
        """Update an existing config.

        TP/SL can be specified as either percentage or absolute price.
        Percentages are converted to prices and stored as prices.
        """
        if config_id not in self.configs:
            raise ValueError(f"Config not found: {config_id}")

        config = self.configs[config_id]

        # Convert TP percentage to price if provided
        if 'take_profit_pct' in kwargs:
            tp_pct = kwargs.pop('take_profit_pct')
            if tp_pct is not None:
                kwargs['take_profit_price'] = config.entry_price * (1 + tp_pct)
            else:
                kwargs['take_profit_price'] = None

        # Convert SL percentage to price if provided
        if 'stop_loss_pct' in kwargs:
            sl_pct = kwargs.pop('stop_loss_pct')
            if sl_pct is not None:
                kwargs['stop_loss_price'] = config.entry_price * (1 - sl_pct)
            else:
                kwargs['stop_loss_price'] = None

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

    def get(self, config_id: str) -> Optional[PositionConfig]:
        """Get a config by ID."""
        return self.configs.get(config_id)

    def get_by_token(self, token_id: str) -> Optional[PositionConfig]:
        """Get a config by token ID."""
        for config in self.configs.values():
            if config.token_id == token_id:
                return config
        return None

    def list_all(self) -> list[PositionConfig]:
        """List all configs."""
        return list(self.configs.values())

    def list_enabled(self) -> list[PositionConfig]:
        """List only enabled configs."""
        return [c for c in self.configs.values() if c.enabled]

    # PID file management for monitor process
    def get_monitor_pid(self) -> Optional[int]:
        """Get the PID of the running monitor, if any."""
        if not PID_FILE.exists():
            return None

        try:
            pid = int(PID_FILE.read_text().strip())
            # Check if process is actually running
            os.kill(pid, 0)
            return pid
        except (ValueError, ProcessLookupError, PermissionError):
            # Process not running, clean up stale PID file
            PID_FILE.unlink(missing_ok=True)
            return None

    def set_monitor_pid(self, pid: int):
        """Set the monitor PID."""
        self._ensure_config_dir()
        PID_FILE.write_text(str(pid))

    def clear_monitor_pid(self):
        """Clear the monitor PID file."""
        PID_FILE.unlink(missing_ok=True)

    def is_monitor_running(self) -> bool:
        """Check if monitor is running."""
        return self.get_monitor_pid() is not None


# Singleton instance
_manager = None

def get_manager() -> MonitorConfigManager:
    """Get the singleton config manager."""
    global _manager
    if _manager is None:
        _manager = MonitorConfigManager()
    return _manager
