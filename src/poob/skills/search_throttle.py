"""Global rate limiter for web search requests (Brave Search, etc.)."""

from __future__ import annotations

import asyncio
import time


class SearchRateLimiter:
    """Limits outgoing search requests to avoid 429 rate-limit responses.

    Uses a lock + minimum delay between requests to serialize access.
    Both EbayLookupTool and RetailLookupTool should share a single instance.

    Args:
        min_delay: Minimum seconds between consecutive requests.
    """

    def __init__(self, min_delay: float = 1.2) -> None:
        self._min_delay = min_delay
        self._lock = asyncio.Lock()
        self._last_request: float = 0.0

    async def acquire(self) -> None:
        """Wait until it's safe to make the next request."""
        async with self._lock:
            now = time.monotonic()
            wait = self._min_delay - (now - self._last_request)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request = time.monotonic()
