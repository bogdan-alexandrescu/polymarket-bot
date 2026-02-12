"""
Scan History Manager - Stores and retrieves historical scan results in PostgreSQL.

Retention is based on the max_hours parameter used for each scan.
"""

import json
import time
from datetime import datetime
from typing import Optional
from dataclasses import dataclass
import uuid

from db import execute


@dataclass
class ScanRecord:
    """A single scan run record."""
    scan_id: str
    timestamp: float
    scan_type: str  # 'quick' or 'deep'
    parameters: dict
    retention_hours: float
    expires_at: float
    opportunities_count: int
    stats: dict
    opportunities: list


class ScanHistoryManager:
    """Manages historical scan results with automatic expiration."""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self._cleanup_expired()

    def _cleanup_expired(self):
        """Remove expired scan records."""
        try:
            execute("DELETE FROM scan_history WHERE expires_at < %s", (time.time(),))
        except Exception:
            pass

    def save_scan(
        self,
        scan_type: str,
        parameters: dict,
        retention_hours: float,
        opportunities: list,
        stats: dict,
    ) -> str:
        self._cleanup_expired()

        now = time.time()
        scan_id = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"

        execute(
            """INSERT INTO scan_history
               (scan_id, timestamp, scan_type, parameters, retention_hours,
                expires_at, opportunities_count, stats, opportunities)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (scan_id, now, scan_type,
             json.dumps(parameters), retention_hours,
             now + (retention_hours * 3600),
             len(opportunities),
             json.dumps(stats),
             json.dumps(opportunities)),
        )

        return scan_id

    def get_scan(self, scan_id: str) -> Optional[ScanRecord]:
        self._cleanup_expired()
        row = execute(
            "SELECT * FROM scan_history WHERE scan_id = %s",
            (scan_id,), fetchone=True,
        )
        if not row:
            return None
        return ScanRecord(
            scan_id=row['scan_id'],
            timestamp=row['timestamp'],
            scan_type=row['scan_type'],
            parameters=row['parameters'] if isinstance(row['parameters'], dict) else json.loads(row['parameters']),
            retention_hours=row['retention_hours'],
            expires_at=row['expires_at'],
            opportunities_count=row['opportunities_count'],
            stats=row['stats'] if isinstance(row['stats'], dict) else json.loads(row['stats']),
            opportunities=row['opportunities'] if isinstance(row['opportunities'], list) else json.loads(row['opportunities']),
        )

    def list_scans(self) -> list[dict]:
        self._cleanup_expired()

        rows = execute(
            """SELECT scan_id, timestamp, scan_type, parameters, retention_hours,
                      expires_at, opportunities_count, stats
               FROM scan_history ORDER BY timestamp DESC""",
            fetch=True,
        )

        summaries = []
        for row in rows:
            params = row['parameters'] if isinstance(row['parameters'], dict) else json.loads(row['parameters'])
            stats = row['stats'] if isinstance(row['stats'], dict) else json.loads(row['stats'])

            summaries.append({
                'scan_id': row['scan_id'],
                'timestamp': row['timestamp'],
                'time_ago': self._format_time_ago(row['timestamp']),
                'scan_type': row['scan_type'],
                'parameters': params,
                'opportunities_count': row['opportunities_count'],
                'retention_hours': row['retention_hours'],
                'expires_at': row['expires_at'],
                'expires_in': self._format_time_remaining(row['expires_at']),
                'stats_summary': {
                    'markets_fetched': stats.get('markets_fetched', 0),
                    'markets_analyzed': stats.get('markets_analyzed', 0),
                    'triage_passed': stats.get('triage_passed', 0),
                    'deep_researched': stats.get('deep_researched', 0),
                },
            })

        return summaries

    def delete_scan(self, scan_id: str) -> bool:
        row = execute("SELECT scan_id FROM scan_history WHERE scan_id = %s", (scan_id,), fetchone=True)
        if not row:
            return False
        execute("DELETE FROM scan_history WHERE scan_id = %s", (scan_id,))
        return True

    def clear_all(self):
        execute("DELETE FROM scan_history")

    def _format_time_ago(self, timestamp: float) -> str:
        diff = time.time() - timestamp
        if diff < 60:
            return "just now"
        elif diff < 3600:
            mins = int(diff / 60)
            return f"{mins} min{'s' if mins > 1 else ''} ago"
        elif diff < 86400:
            hours = int(diff / 3600)
            return f"{hours} hour{'s' if hours > 1 else ''} ago"
        else:
            days = int(diff / 86400)
            return f"{days} day{'s' if days > 1 else ''} ago"

    def _format_time_remaining(self, expires_at: float) -> str:
        diff = expires_at - time.time()
        if diff <= 0:
            return "expired"
        elif diff < 3600:
            mins = int(diff / 60)
            return f"{mins} min{'s' if mins > 1 else ''}"
        elif diff < 86400:
            hours = int(diff / 3600)
            return f"{hours} hour{'s' if hours > 1 else ''}"
        else:
            days = int(diff / 86400)
            return f"{days} day{'s' if days > 1 else ''}"


# Global instance
scan_history = ScanHistoryManager()
