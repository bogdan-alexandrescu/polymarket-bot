"""
API Cache - Caching layer for Claude/Anthropic API responses.

Uses PostgreSQL as source of truth with an in-memory L1 cache.
"""

import hashlib
import time
import asyncio
import json
from dataclasses import dataclass
from typing import Optional, Any


@dataclass
class CacheEntry:
    """A cached API response."""
    key: str
    data: Any
    created_at: float
    expires_at: float
    cache_type: str


class APICache:
    """DB-backed cache for API responses with in-memory L1 and TTL support."""

    def __init__(self, default_ttl_seconds: int = 7200):
        self.default_ttl = default_ttl_seconds
        self._memory_cache: dict[str, CacheEntry] = {}

    def _get_cache_key(self, cache_type: str, identifier: str) -> str:
        hash_str = hashlib.md5(identifier.encode()).hexdigest()[:16]
        return f"{cache_type}_{hash_str}"

    def get(self, cache_type: str, identifier: str) -> Optional[Any]:
        key = self._get_cache_key(cache_type, identifier)
        now = time.time()

        # Check memory cache first
        if key in self._memory_cache:
            entry = self._memory_cache[key]
            if entry.expires_at > now:
                return entry.data
            else:
                del self._memory_cache[key]

        # Check DB
        try:
            from db import execute
            row = execute(
                "SELECT data, expires_at, cache_type FROM api_cache WHERE key = %s AND expires_at > %s",
                (key, now), fetchone=True,
            )
            if row:
                data = row['data']
                self._memory_cache[key] = CacheEntry(
                    key=key,
                    data=data,
                    created_at=now,
                    expires_at=row['expires_at'],
                    cache_type=row['cache_type'],
                )
                return data
        except Exception:
            pass

        return None

    def set(self, cache_type: str, identifier: str, data: Any, ttl_seconds: int = None):
        if ttl_seconds is None:
            ttl_seconds = self.default_ttl

        key = self._get_cache_key(cache_type, identifier)
        now = time.time()

        entry = CacheEntry(
            key=key,
            data=data,
            created_at=now,
            expires_at=now + ttl_seconds,
            cache_type=cache_type,
        )

        self._memory_cache[key] = entry

        try:
            from db import execute
            execute(
                """INSERT INTO api_cache (key, data, cache_type, created_at, expires_at)
                   VALUES (%s, %s, %s, %s, %s)
                   ON CONFLICT (key) DO UPDATE SET
                   data = EXCLUDED.data, cache_type = EXCLUDED.cache_type,
                   created_at = EXCLUDED.created_at, expires_at = EXCLUDED.expires_at""",
                (key, json.dumps(data), cache_type, now, now + ttl_seconds),
            )
        except Exception:
            pass

    def clear_expired(self):
        now = time.time()
        expired_keys = [
            key for key, entry in self._memory_cache.items()
            if entry.expires_at <= now
        ]
        for key in expired_keys:
            del self._memory_cache[key]

        try:
            from db import execute
            execute("DELETE FROM api_cache WHERE expires_at < %s", (now,))
        except Exception:
            pass

        return len(expired_keys)

    def clear_all(self):
        self._memory_cache.clear()
        try:
            from db import execute
            execute("DELETE FROM api_cache")
        except Exception:
            pass

    def get_stats(self) -> dict:
        now = time.time()
        total = len(self._memory_cache)
        expired = sum(1 for e in self._memory_cache.values() if e.expires_at <= now)
        valid = total - expired

        by_type = {}
        for entry in self._memory_cache.values():
            if entry.expires_at > now:
                by_type[entry.cache_type] = by_type.get(entry.cache_type, 0) + 1

        return {
            'total_entries': total,
            'valid_entries': valid,
            'expired_entries': expired,
            'by_type': by_type,
            'default_ttl_hours': self.default_ttl / 3600,
        }


class RateLimiter:
    """Rate limiter to avoid hitting API limits."""

    def __init__(
        self,
        requests_per_minute: int = 10,
        min_delay_seconds: float = 3.0,
    ):
        self.requests_per_minute = requests_per_minute
        self.min_delay = min_delay_seconds
        self.request_times: list[float] = []
        self._lock = asyncio.Lock()
        self._rate_limit_until: float = 0
        self._consecutive_rate_limits: int = 0

    async def acquire(self):
        async with self._lock:
            now = time.time()

            if now < self._rate_limit_until:
                wait_time = self._rate_limit_until - now
                print(f"Rate limit cooldown: waiting {wait_time:.1f}s...")
                await asyncio.sleep(wait_time)
                now = time.time()

            self.request_times = [t for t in self.request_times if now - t < 60]

            if len(self.request_times) >= self.requests_per_minute:
                oldest = self.request_times[0]
                wait_time = 60 - (now - oldest) + 1.0
                if wait_time > 0:
                    print(f"Rate limit: waiting {wait_time:.1f}s (at {len(self.request_times)}/{self.requests_per_minute} rpm)...")
                    await asyncio.sleep(wait_time)
                    now = time.time()
                    self.request_times = [t for t in self.request_times if now - t < 60]

            if self.request_times:
                last_request = self.request_times[-1]
                time_since_last = now - last_request
                if time_since_last < self.min_delay:
                    await asyncio.sleep(self.min_delay - time_since_last)

            self.request_times.append(time.time())

    def report_rate_limit_error(self, retry_after: float = None):
        self._consecutive_rate_limits += 1
        base_wait = retry_after if retry_after else 30
        backoff_multiplier = min(2 ** (self._consecutive_rate_limits - 1), 8)
        wait_time = min(base_wait * backoff_multiplier, 300)
        self._rate_limit_until = time.time() + wait_time
        print(f"Rate limit reported (#{self._consecutive_rate_limits}). Cooldown for {wait_time:.0f}s")

    def report_success(self):
        self._consecutive_rate_limits = 0

    def is_rate_limited(self) -> bool:
        return time.time() < self._rate_limit_until

    def get_stats(self) -> dict:
        now = time.time()
        recent = [t for t in self.request_times if now - t < 60]
        return {
            'requests_last_minute': len(recent),
            'requests_per_minute_limit': self.requests_per_minute,
            'min_delay_seconds': self.min_delay,
            'rate_limited': self.is_rate_limited(),
            'cooldown_remaining': max(0, self._rate_limit_until - now),
            'consecutive_rate_limits': self._consecutive_rate_limits,
        }


# Global instances
_cache_instance: Optional[APICache] = None
_rate_limiter_instance: Optional[RateLimiter] = None


def get_cache(ttl_hours: float = 2.0) -> APICache:
    global _cache_instance
    if _cache_instance is None:
        _cache_instance = APICache(default_ttl_seconds=int(ttl_hours * 3600))
    return _cache_instance


def get_rate_limiter(requests_per_minute: int = 50) -> RateLimiter:
    global _rate_limiter_instance
    if _rate_limiter_instance is None:
        _rate_limiter_instance = RateLimiter(requests_per_minute=requests_per_minute)
    return _rate_limiter_instance
