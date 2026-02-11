"""
Scan History Manager - Stores and retrieves historical scan results.

Retention is based on the max_hours parameter used for each scan.
"""

import json
import os
import time
import threading
from datetime import datetime, timezone
from typing import Optional
from dataclasses import dataclass, asdict
import uuid


@dataclass
class ScanRecord:
    """A single scan run record."""
    scan_id: str
    timestamp: float
    scan_type: str  # 'quick' or 'deep'
    parameters: dict  # hours, risk, top, max_ai, etc.
    retention_hours: float
    expires_at: float
    opportunities_count: int
    stats: dict
    opportunities: list  # Full opportunity data


class ScanHistoryManager:
    """Manages historical scan results with automatic expiration."""

    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return

        self._initialized = True
        self.storage_path = os.path.join(
            os.path.dirname(__file__),
            'data',
            'scan_history.json'
        )
        self.scans: dict[str, ScanRecord] = {}
        self._load()
        self._cleanup_expired()

    def _ensure_data_dir(self):
        """Ensure data directory exists."""
        data_dir = os.path.dirname(self.storage_path)
        if not os.path.exists(data_dir):
            os.makedirs(data_dir)

    def _load(self):
        """Load scan history from disk."""
        try:
            if os.path.exists(self.storage_path):
                with open(self.storage_path, 'r') as f:
                    data = json.load(f)
                    for scan_id, scan_data in data.items():
                        self.scans[scan_id] = ScanRecord(**scan_data)
        except Exception as e:
            print(f"Error loading scan history: {e}")
            self.scans = {}

    def _save(self):
        """Save scan history to disk."""
        try:
            self._ensure_data_dir()
            with open(self.storage_path, 'w') as f:
                data = {
                    scan_id: asdict(record)
                    for scan_id, record in self.scans.items()
                }
                json.dump(data, f, indent=2)
        except Exception as e:
            print(f"Error saving scan history: {e}")

    def _cleanup_expired(self):
        """Remove expired scan records."""
        now = time.time()
        expired = [
            scan_id for scan_id, record in self.scans.items()
            if record.expires_at < now
        ]
        for scan_id in expired:
            del self.scans[scan_id]
        if expired:
            self._save()

    def save_scan(
        self,
        scan_type: str,
        parameters: dict,
        retention_hours: float,
        opportunities: list,
        stats: dict,
    ) -> str:
        """
        Save a scan result.

        Args:
            scan_type: 'quick' or 'deep'
            parameters: Scan parameters (hours, risk, top, max_ai)
            retention_hours: How long to keep this scan (usually same as max_hours)
            opportunities: List of opportunity dicts
            stats: Scan statistics

        Returns:
            scan_id: Unique ID for this scan
        """
        self._cleanup_expired()

        now = time.time()
        scan_id = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"

        record = ScanRecord(
            scan_id=scan_id,
            timestamp=now,
            scan_type=scan_type,
            parameters=parameters,
            retention_hours=retention_hours,
            expires_at=now + (retention_hours * 3600),
            opportunities_count=len(opportunities),
            stats=stats,
            opportunities=opportunities,
        )

        self.scans[scan_id] = record
        self._save()

        return scan_id

    def get_scan(self, scan_id: str) -> Optional[ScanRecord]:
        """Get a specific scan by ID."""
        self._cleanup_expired()
        return self.scans.get(scan_id)

    def list_scans(self) -> list[dict]:
        """
        List all non-expired scans (summary only, not full opportunities).

        Returns list sorted by timestamp (newest first).
        """
        self._cleanup_expired()

        summaries = []
        for scan_id, record in self.scans.items():
            time_ago = self._format_time_ago(record.timestamp)
            expires_in = self._format_time_remaining(record.expires_at)

            summaries.append({
                'scan_id': record.scan_id,
                'timestamp': record.timestamp,
                'time_ago': time_ago,
                'scan_type': record.scan_type,
                'parameters': record.parameters,
                'opportunities_count': record.opportunities_count,
                'retention_hours': record.retention_hours,
                'expires_at': record.expires_at,
                'expires_in': expires_in,
                'stats_summary': {
                    'markets_fetched': record.stats.get('markets_fetched', 0),
                    'markets_analyzed': record.stats.get('markets_analyzed', 0),
                    'triage_passed': record.stats.get('triage_passed', 0),
                    'deep_researched': record.stats.get('deep_researched', 0),
                },
            })

        # Sort by timestamp, newest first
        summaries.sort(key=lambda x: x['timestamp'], reverse=True)
        return summaries

    def delete_scan(self, scan_id: str) -> bool:
        """Delete a specific scan."""
        if scan_id in self.scans:
            del self.scans[scan_id]
            self._save()
            return True
        return False

    def clear_all(self):
        """Clear all scan history."""
        self.scans = {}
        self._save()

    def _format_time_ago(self, timestamp: float) -> str:
        """Format timestamp as 'X minutes/hours ago'."""
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
        """Format expiration as 'X minutes/hours remaining'."""
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
