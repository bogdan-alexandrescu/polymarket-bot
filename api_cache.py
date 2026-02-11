"""
API Cache - Caching layer for Claude/Anthropic API responses.

Provides file-based caching with configurable TTL to reduce API costs
and avoid rate limits.
"""

import os
import json
import hashlib
import time
import asyncio
from pathlib import Path
from dataclasses import dataclass, asdict
from typing import Optional, Any
from datetime import datetime, timezone


@dataclass
class CacheEntry:
    """A cached API response."""
    key: str
    data: Any
    created_at: float  # Unix timestamp
    expires_at: float  # Unix timestamp
    cache_type: str  # "facts", "analysis", "deep_research"


class APICache:
    """File-based cache for API responses with TTL support."""

    def __init__(
        self,
        cache_dir: str = None,
        default_ttl_seconds: int = 7200,  # 2 hours default
    ):
        if cache_dir is None:
            cache_dir = os.path.join(os.path.dirname(__file__), ".api_cache")

        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.default_ttl = default_ttl_seconds

        # In-memory cache for faster access
        self._memory_cache: dict[str, CacheEntry] = {}

        # Load existing cache entries into memory
        self._load_cache_index()

    def _load_cache_index(self):
        """Load cache index from disk."""
        index_file = self.cache_dir / "index.json"
        if index_file.exists():
            try:
                with open(index_file, 'r') as f:
                    index = json.load(f)
                    # Only load non-expired entries
                    now = time.time()
                    for key, meta in index.items():
                        if meta.get('expires_at', 0) > now:
                            self._memory_cache[key] = CacheEntry(
                                key=key,
                                data=None,  # Lazy load data
                                created_at=meta['created_at'],
                                expires_at=meta['expires_at'],
                                cache_type=meta.get('cache_type', 'unknown'),
                            )
            except (json.JSONDecodeError, KeyError):
                pass

    def _save_cache_index(self):
        """Save cache index to disk."""
        index_file = self.cache_dir / "index.json"
        index = {}
        for key, entry in self._memory_cache.items():
            index[key] = {
                'created_at': entry.created_at,
                'expires_at': entry.expires_at,
                'cache_type': entry.cache_type,
            }
        with open(index_file, 'w') as f:
            json.dump(index, f)

    def _get_cache_key(self, cache_type: str, identifier: str) -> str:
        """Generate a cache key from type and identifier."""
        # Hash the identifier to create a safe filename
        hash_str = hashlib.md5(identifier.encode()).hexdigest()[:16]
        return f"{cache_type}_{hash_str}"

    def _get_cache_file(self, key: str) -> Path:
        """Get the file path for a cache entry."""
        return self.cache_dir / f"{key}.json"

    def get(
        self,
        cache_type: str,
        identifier: str,
    ) -> Optional[Any]:
        """
        Get a cached value if it exists and hasn't expired.

        Args:
            cache_type: Type of cache (e.g., "facts", "analysis")
            identifier: Unique identifier (e.g., market question)

        Returns:
            Cached data or None if not found/expired
        """
        key = self._get_cache_key(cache_type, identifier)

        # Check memory cache first
        if key in self._memory_cache:
            entry = self._memory_cache[key]
            if entry.expires_at > time.time():
                # Load data from disk if not in memory
                if entry.data is None:
                    cache_file = self._get_cache_file(key)
                    if cache_file.exists():
                        try:
                            with open(cache_file, 'r') as f:
                                entry.data = json.load(f)
                        except (json.JSONDecodeError, IOError):
                            return None
                return entry.data
            else:
                # Expired, remove from cache
                self._remove(key)

        return None

    def set(
        self,
        cache_type: str,
        identifier: str,
        data: Any,
        ttl_seconds: int = None,
    ):
        """
        Store a value in the cache.

        Args:
            cache_type: Type of cache
            identifier: Unique identifier
            data: Data to cache (must be JSON serializable)
            ttl_seconds: Time to live in seconds (uses default if not specified)
        """
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

        # Save to memory
        self._memory_cache[key] = entry

        # Save to disk
        cache_file = self._get_cache_file(key)
        try:
            with open(cache_file, 'w') as f:
                json.dump(data, f)
            self._save_cache_index()
        except IOError as e:
            print(f"Cache write error: {e}")

    def _remove(self, key: str):
        """Remove a cache entry."""
        if key in self._memory_cache:
            del self._memory_cache[key]

        cache_file = self._get_cache_file(key)
        if cache_file.exists():
            try:
                cache_file.unlink()
            except IOError:
                pass

        self._save_cache_index()

    def clear_expired(self):
        """Remove all expired entries from the cache."""
        now = time.time()
        expired_keys = [
            key for key, entry in self._memory_cache.items()
            if entry.expires_at <= now
        ]
        for key in expired_keys:
            self._remove(key)
        return len(expired_keys)

    def clear_all(self):
        """Clear all cache entries."""
        for key in list(self._memory_cache.keys()):
            self._remove(key)
        self._memory_cache.clear()
        self._save_cache_index()

    def get_stats(self) -> dict:
        """Get cache statistics."""
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
            'cache_dir': str(self.cache_dir),
            'default_ttl_hours': self.default_ttl / 3600,
        }


class RateLimiter:
    """Rate limiter to avoid hitting API limits."""

    def __init__(
        self,
        requests_per_minute: int = 10,  # Reduced from 50 - web search is expensive
        min_delay_seconds: float = 3.0,  # Increased from 0.5 - more spacing between calls
    ):
        self.requests_per_minute = requests_per_minute
        self.min_delay = min_delay_seconds
        self.request_times: list[float] = []
        self._lock = asyncio.Lock()
        self._rate_limit_until: float = 0  # Timestamp when rate limit expires
        self._consecutive_rate_limits: int = 0  # Track consecutive rate limit errors

    async def acquire(self):
        """Wait if necessary to avoid rate limits."""
        async with self._lock:
            now = time.time()

            # Check if we're in a rate-limited cooldown period
            if now < self._rate_limit_until:
                wait_time = self._rate_limit_until - now
                print(f"Rate limit cooldown: waiting {wait_time:.1f}s...")
                await asyncio.sleep(wait_time)
                now = time.time()

            # Remove requests older than 1 minute
            self.request_times = [t for t in self.request_times if now - t < 60]

            # Check if we're at the limit
            if len(self.request_times) >= self.requests_per_minute:
                # Wait until the oldest request is more than 1 minute old
                oldest = self.request_times[0]
                wait_time = 60 - (now - oldest) + 1.0  # Extra buffer
                if wait_time > 0:
                    print(f"Rate limit: waiting {wait_time:.1f}s (at {len(self.request_times)}/{self.requests_per_minute} rpm)...")
                    await asyncio.sleep(wait_time)
                    now = time.time()
                    # Clean up again after waiting
                    self.request_times = [t for t in self.request_times if now - t < 60]

            # Always wait minimum delay between requests
            if self.request_times:
                last_request = self.request_times[-1]
                time_since_last = now - last_request
                if time_since_last < self.min_delay:
                    await asyncio.sleep(self.min_delay - time_since_last)

            # Record this request
            self.request_times.append(time.time())

    def report_rate_limit_error(self, retry_after: float = None):
        """Report that a rate limit error was received from the API."""
        self._consecutive_rate_limits += 1

        # Calculate backoff time based on consecutive errors
        # 30s, 60s, 120s, 240s... capped at 5 minutes
        base_wait = retry_after if retry_after else 30
        backoff_multiplier = min(2 ** (self._consecutive_rate_limits - 1), 8)
        wait_time = min(base_wait * backoff_multiplier, 300)

        self._rate_limit_until = time.time() + wait_time
        print(f"Rate limit reported (#{self._consecutive_rate_limits}). Cooldown for {wait_time:.0f}s")

    def report_success(self):
        """Report a successful API call to reset consecutive rate limit counter."""
        self._consecutive_rate_limits = 0

    def is_rate_limited(self) -> bool:
        """Check if we're currently in a rate-limited state."""
        return time.time() < self._rate_limit_until

    def get_stats(self) -> dict:
        """Get rate limiter statistics."""
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
    """Get the global cache instance."""
    global _cache_instance
    if _cache_instance is None:
        _cache_instance = APICache(default_ttl_seconds=int(ttl_hours * 3600))
    return _cache_instance


def get_rate_limiter(requests_per_minute: int = 50) -> RateLimiter:
    """Get the global rate limiter instance."""
    global _rate_limiter_instance
    if _rate_limiter_instance is None:
        _rate_limiter_instance = RateLimiter(requests_per_minute=requests_per_minute)
    return _rate_limiter_instance
