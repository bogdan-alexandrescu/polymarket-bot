#!/usr/bin/env python3
"""
Copy Trading Configuration Manager

Handles persistent storage of copy trading configurations in PostgreSQL.
"""

import os
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from db import execute


@dataclass
class CopyTraderConfig:
    """Configuration for a followed trader."""
    id: str
    handle: str
    wallet_address: str
    profile_name: str
    max_amount: float = 5.0
    extra_pct: float = 0.10
    enabled: bool = True
    created_at: str = ""
    updated_at: str = ""
    last_check_timestamp: Optional[float] = None

    def __post_init__(self):
        if not self.created_at:
            self.created_at = datetime.now().isoformat()
        if not self.updated_at:
            self.updated_at = datetime.now().isoformat()

    def summary(self) -> str:
        status = "ON" if self.enabled else "OFF"
        return f"[{self.id}] @{self.handle} ({self.profile_name}) | max ${self.max_amount:.0f} +{self.extra_pct*100:.0f}% | {status}"


def _row_to_config(row: dict) -> CopyTraderConfig:
    """Convert a DB row dict to a CopyTraderConfig dataclass."""
    return CopyTraderConfig(
        id=row['id'],
        handle=row['handle'],
        wallet_address=row['wallet_address'],
        profile_name=row['profile_name'],
        max_amount=row['max_amount'],
        extra_pct=row['extra_pct'],
        enabled=row['enabled'],
        created_at=row['created_at'],
        updated_at=row['updated_at'],
        last_check_timestamp=row.get('last_check_timestamp'),
    )


class CopyTradingConfigManager:
    """Manages copy trading configurations stored in PostgreSQL."""

    def __init__(self):
        pass

    def _generate_id(self) -> str:
        import random
        import string
        return ''.join(random.choices(string.ascii_lowercase + string.digits, k=6))

    def add(self,
            handle: str,
            wallet_address: str,
            profile_name: str,
            max_amount: float = 5.0,
            extra_pct: float = 0.10,
            **kwargs) -> CopyTraderConfig:
        # Check if already following this wallet
        existing = execute(
            "SELECT id, handle FROM copy_trading_configs WHERE LOWER(wallet_address) = LOWER(%s)",
            (wallet_address,), fetchone=True,
        )
        if existing:
            raise ValueError(f"Already following this trader: {existing['handle']} ({existing['id']})")

        config_id = self._generate_id()
        now = datetime.now().isoformat()

        config = CopyTraderConfig(
            id=config_id,
            handle=handle,
            wallet_address=wallet_address,
            profile_name=profile_name,
            max_amount=max_amount,
            extra_pct=extra_pct,
            created_at=now,
            updated_at=now,
        )

        execute(
            """INSERT INTO copy_trading_configs
               (id, handle, wallet_address, profile_name, max_amount, extra_pct,
                enabled, created_at, updated_at, last_check_timestamp)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (config.id, config.handle, config.wallet_address, config.profile_name,
             config.max_amount, config.extra_pct, config.enabled,
             config.created_at, config.updated_at, config.last_check_timestamp),
        )
        return config

    def update(self, config_id: str, **kwargs) -> CopyTraderConfig:
        row = execute("SELECT * FROM copy_trading_configs WHERE id = %s", (config_id,), fetchone=True)
        if not row:
            raise ValueError(f"Config not found: {config_id}")

        config = _row_to_config(row)
        for key, value in kwargs.items():
            if hasattr(config, key):
                setattr(config, key, value)

        config.updated_at = datetime.now().isoformat()

        execute(
            """UPDATE copy_trading_configs SET
               handle=%s, wallet_address=%s, profile_name=%s,
               max_amount=%s, extra_pct=%s, enabled=%s,
               updated_at=%s, last_check_timestamp=%s
               WHERE id=%s""",
            (config.handle, config.wallet_address, config.profile_name,
             config.max_amount, config.extra_pct, config.enabled,
             config.updated_at, config.last_check_timestamp,
             config_id),
        )
        return config

    def delete(self, config_id: str) -> bool:
        row = execute("SELECT id FROM copy_trading_configs WHERE id = %s", (config_id,), fetchone=True)
        if not row:
            return False
        execute("DELETE FROM copy_trading_configs WHERE id = %s", (config_id,))
        return True

    def get(self, config_id: str) -> Optional[CopyTraderConfig]:
        row = execute("SELECT * FROM copy_trading_configs WHERE id = %s", (config_id,), fetchone=True)
        return _row_to_config(row) if row else None

    def list_all(self) -> list[CopyTraderConfig]:
        rows = execute("SELECT * FROM copy_trading_configs ORDER BY created_at", fetch=True)
        return [_row_to_config(r) for r in rows]

    def list_enabled(self) -> list[CopyTraderConfig]:
        rows = execute(
            "SELECT * FROM copy_trading_configs WHERE enabled = TRUE ORDER BY created_at",
            fetch=True,
        )
        return [_row_to_config(r) for r in rows]

    # PID management via daemon_state table
    def get_pid(self) -> Optional[int]:
        row = execute(
            "SELECT pid FROM daemon_state WHERE daemon_name = 'copy_trader'",
            fetchone=True,
        )
        if not row or row['pid'] is None:
            return None
        pid = row['pid']
        try:
            os.kill(pid, 0)
            return pid
        except (ProcessLookupError, PermissionError):
            self.clear_pid()
            return None

    def set_pid(self, pid: int):
        execute(
            """UPDATE daemon_state SET pid = %s, started_at = EXTRACT(EPOCH FROM NOW()),
               last_heartbeat = EXTRACT(EPOCH FROM NOW())
               WHERE daemon_name = 'copy_trader'""",
            (pid,),
        )

    def clear_pid(self):
        execute(
            "UPDATE daemon_state SET pid = NULL WHERE daemon_name = 'copy_trader'",
        )

    def is_running(self) -> bool:
        return self.get_pid() is not None


# Singleton instance
_ct_manager = None

def get_ct_manager() -> CopyTradingConfigManager:
    global _ct_manager
    if _ct_manager is None:
        _ct_manager = CopyTradingConfigManager()
    return _ct_manager
