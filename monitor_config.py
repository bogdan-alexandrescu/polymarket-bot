#!/usr/bin/env python3
"""
Monitor Configuration Manager

Handles persistent storage of take-profit (TP) and stop-loss (SL) configurations
in PostgreSQL.
"""

import os
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from db import execute


@dataclass
class PositionConfig:
    """Configuration for a monitored position."""
    id: str
    token_id: str
    name: str
    side: str  # "Yes" or "No"
    shares: float
    entry_price: float

    # Market info
    description: str = ""
    slug: str = ""

    # Take profit config (optional)
    take_profit_pct: Optional[float] = None
    take_profit_price: Optional[float] = None

    # Stop loss config (optional)
    stop_loss_pct: Optional[float] = None
    stop_loss_price: Optional[float] = None

    # Status
    enabled: bool = True
    created_at: str = ""
    updated_at: str = ""

    def __post_init__(self):
        if not self.created_at:
            self.created_at = datetime.now().isoformat()
        if not self.updated_at:
            self.updated_at = datetime.now().isoformat()

    def get_tp_target(self) -> Optional[float]:
        if self.take_profit_price:
            return self.take_profit_price
        if self.take_profit_pct:
            return self.entry_price * (1 + self.take_profit_pct)
        return None

    def get_sl_target(self) -> Optional[float]:
        if self.stop_loss_price:
            return self.stop_loss_price
        if self.stop_loss_pct:
            return self.entry_price * (1 - self.stop_loss_pct)
        return None

    def summary(self) -> str:
        tp = self.get_tp_target()
        sl = self.get_sl_target()
        tp_str = f"TP: {tp*100:.1f}%" if tp else "TP: -"
        sl_str = f"SL: {sl*100:.1f}%" if sl else "SL: -"
        status = "ON" if self.enabled else "OFF"
        return f"[{self.id}] {self.name} ({self.side}) | Entry: {self.entry_price*100:.1f}% | {tp_str} | {sl_str} | {status}"


def _row_to_config(row: dict) -> PositionConfig:
    """Convert a DB row dict to a PositionConfig dataclass."""
    return PositionConfig(
        id=row['id'],
        token_id=row['token_id'],
        name=row['name'],
        side=row['side'],
        shares=row['shares'],
        entry_price=row['entry_price'],
        description=row.get('description', ''),
        slug=row.get('slug', ''),
        take_profit_pct=row.get('take_profit_pct'),
        take_profit_price=row.get('take_profit_price'),
        stop_loss_pct=row.get('stop_loss_pct'),
        stop_loss_price=row.get('stop_loss_price'),
        enabled=row['enabled'],
        created_at=row['created_at'],
        updated_at=row['updated_at'],
    )


class MonitorConfigManager:
    """Manages monitor configurations stored in PostgreSQL."""

    def __init__(self):
        pass

    def _generate_id(self) -> str:
        import random
        import string
        return ''.join(random.choices(string.ascii_lowercase + string.digits, k=6))

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
        # Check if already exists for this token
        existing = execute(
            "SELECT id FROM monitor_configs WHERE token_id = %s",
            (token_id,), fetchone=True,
        )
        if existing:
            raise ValueError(f"Config already exists for this token: {existing['id']}")

        config_id = self._generate_id()

        # Convert percentages to prices (store only prices)
        final_tp_price = take_profit_price
        if take_profit_pct is not None and final_tp_price is None:
            final_tp_price = entry_price * (1 + take_profit_pct)

        final_sl_price = stop_loss_price
        if stop_loss_pct is not None and final_sl_price is None:
            final_sl_price = entry_price * (1 - stop_loss_pct)

        now = datetime.now().isoformat()
        config = PositionConfig(
            id=config_id,
            token_id=token_id,
            name=name,
            side=side,
            shares=shares,
            entry_price=entry_price,
            description=description,
            slug=slug,
            take_profit_pct=None,
            take_profit_price=final_tp_price,
            stop_loss_pct=None,
            stop_loss_price=final_sl_price,
            created_at=now,
            updated_at=now,
        )

        execute(
            """INSERT INTO monitor_configs
               (id, token_id, name, side, shares, entry_price, description, slug,
                take_profit_pct, take_profit_price, stop_loss_pct, stop_loss_price,
                enabled, created_at, updated_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (config.id, config.token_id, config.name, config.side,
             config.shares, config.entry_price, config.description, config.slug,
             config.take_profit_pct, config.take_profit_price,
             config.stop_loss_pct, config.stop_loss_price,
             config.enabled, config.created_at, config.updated_at),
        )
        return config

    def update(self, config_id: str, **kwargs) -> PositionConfig:
        row = execute("SELECT * FROM monitor_configs WHERE id = %s", (config_id,), fetchone=True)
        if not row:
            raise ValueError(f"Config not found: {config_id}")

        config = _row_to_config(row)

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

        execute(
            """UPDATE monitor_configs SET
               token_id=%s, name=%s, side=%s, shares=%s, entry_price=%s,
               description=%s, slug=%s,
               take_profit_pct=%s, take_profit_price=%s,
               stop_loss_pct=%s, stop_loss_price=%s,
               enabled=%s, updated_at=%s
               WHERE id=%s""",
            (config.token_id, config.name, config.side, config.shares,
             config.entry_price, config.description, config.slug,
             config.take_profit_pct, config.take_profit_price,
             config.stop_loss_pct, config.stop_loss_price,
             config.enabled, config.updated_at,
             config_id),
        )
        return config

    def delete(self, config_id: str) -> bool:
        row = execute("SELECT id FROM monitor_configs WHERE id = %s", (config_id,), fetchone=True)
        if not row:
            return False
        execute("DELETE FROM monitor_configs WHERE id = %s", (config_id,))
        return True

    def get(self, config_id: str) -> Optional[PositionConfig]:
        row = execute("SELECT * FROM monitor_configs WHERE id = %s", (config_id,), fetchone=True)
        return _row_to_config(row) if row else None

    def get_by_token(self, token_id: str) -> Optional[PositionConfig]:
        row = execute("SELECT * FROM monitor_configs WHERE token_id = %s", (token_id,), fetchone=True)
        return _row_to_config(row) if row else None

    def list_all(self) -> list[PositionConfig]:
        rows = execute("SELECT * FROM monitor_configs ORDER BY created_at", fetch=True)
        return [_row_to_config(r) for r in rows]

    def list_enabled(self) -> list[PositionConfig]:
        rows = execute(
            "SELECT * FROM monitor_configs WHERE enabled = TRUE ORDER BY created_at",
            fetch=True,
        )
        return [_row_to_config(r) for r in rows]

    # PID management via daemon_state table
    def get_monitor_pid(self) -> Optional[int]:
        row = execute(
            "SELECT pid FROM daemon_state WHERE daemon_name = 'profit_monitor'",
            fetchone=True,
        )
        if not row or row['pid'] is None:
            return None
        pid = row['pid']
        try:
            os.kill(pid, 0)
            return pid
        except (ProcessLookupError, PermissionError):
            self.clear_monitor_pid()
            return None

    def set_monitor_pid(self, pid: int):
        execute(
            """UPDATE daemon_state SET pid = %s, started_at = EXTRACT(EPOCH FROM NOW()),
               last_heartbeat = EXTRACT(EPOCH FROM NOW())
               WHERE daemon_name = 'profit_monitor'""",
            (pid,),
        )

    def clear_monitor_pid(self):
        execute(
            "UPDATE daemon_state SET pid = NULL WHERE daemon_name = 'profit_monitor'",
        )

    def is_monitor_running(self) -> bool:
        return self.get_monitor_pid() is not None


# Singleton instance
_manager = None

def get_manager() -> MonitorConfigManager:
    global _manager
    if _manager is None:
        _manager = MonitorConfigManager()
    return _manager
