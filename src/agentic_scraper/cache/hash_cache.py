"""Perceptual hash cache for deduplicating API calls on similar images.

Uses diskcache (SQLite-backed) with imagehash for perceptual hashing.
Caches Vision API, SerpAPI, VLM evaluations, and eBay lookup results
keyed by the image's perceptual hash — so visually identical images
(even at different resolutions/compressions) hit the cache.
"""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
from typing import Any

import diskcache
import imagehash
from PIL import Image

from agentic_scraper.utils.logging import get_logger

log = get_logger("cache.hash_cache")

# Default cache directory
_DEFAULT_CACHE_DIR = Path("data/cache")

# TTLs in seconds
VISION_TTL = 30 * 24 * 3600       # 30 days
SERPAPI_TTL = 30 * 24 * 3600       # 30 days
VLM_TTL = 14 * 24 * 3600          # 14 days
EBAY_TTL = 10 * 24 * 3600         # 10 days
SEARCH_TTL = 7 * 24 * 3600        # 7 days


def compute_phash(image_bytes: bytes) -> str:
    """Compute perceptual hash of image bytes.

    Args:
        image_bytes: Raw image data.

    Returns:
        Hex string of the perceptual hash.
    """
    try:
        img = Image.open(io.BytesIO(image_bytes))
        return str(imagehash.phash(img))
    except Exception:
        # Fallback to content hash if image parsing fails
        return hashlib.sha256(image_bytes).hexdigest()[:16]


def compute_text_hash(text: str) -> str:
    """Compute a deterministic hash for text content (queries, prompts).

    Args:
        text: Text to hash.

    Returns:
        Short hex hash string.
    """
    return hashlib.sha256(text.encode()).hexdigest()[:16]


class ResultCache:
    """SQLite-backed cache for API results keyed by perceptual or content hash.

    Uses diskcache for persistent, thread-safe caching with TTL support.
    Each cache namespace (vision, serpapi, vlm, ebay) gets its own
    key prefix to avoid collisions.

    Args:
        cache_dir: Directory for the diskcache SQLite database.
        size_limit: Maximum cache size in bytes (default 1GB).
    """

    def __init__(
        self,
        cache_dir: Path = _DEFAULT_CACHE_DIR,
        size_limit: int = 1_073_741_824,
    ) -> None:
        cache_dir.mkdir(parents=True, exist_ok=True)
        self._cache = diskcache.Cache(
            str(cache_dir),
            size_limit=size_limit,
            eviction_policy="least-recently-used",
        )
        log.info("Result cache initialized", path=str(cache_dir))

    def get(self, namespace: str, key: str) -> Any | None:
        """Get a cached result.

        Args:
            namespace: Cache namespace (e.g. "vision", "vlm", "ebay").
            key: Cache key (perceptual hash or content hash).

        Returns:
            Cached data if found and not expired, else None.
        """
        full_key = f"{namespace}:{key}"
        result = self._cache.get(full_key)
        if result is not None:
            log.debug("cache.hit", namespace=namespace, key=key[:16])
        return result

    def set(
        self, namespace: str, key: str, value: Any, ttl: int | None = None
    ) -> None:
        """Store a result in the cache.

        Args:
            namespace: Cache namespace.
            key: Cache key.
            value: Data to cache (must be picklable or JSON-serializable).
            ttl: Time-to-live in seconds. None = no expiry.
        """
        full_key = f"{namespace}:{key}"
        self._cache.set(full_key, value, expire=ttl)
        log.debug("cache.set", namespace=namespace, key=key[:16])

    def get_vision(self, phash: str) -> dict | None:
        """Get cached Vision API result by image perceptual hash."""
        return self.get("vision", phash)

    def set_vision(self, phash: str, result: dict) -> None:
        """Cache Vision API result."""
        self.set("vision", phash, result, ttl=VISION_TTL)

    def get_serpapi(self, phash: str) -> dict | None:
        """Get cached SerpAPI result by image perceptual hash."""
        return self.get("serpapi", phash)

    def set_serpapi(self, phash: str, result: dict) -> None:
        """Cache SerpAPI result."""
        self.set("serpapi", phash, result, ttl=SERPAPI_TTL)

    def get_vlm(self, phash: str, prompt_hash: str) -> dict | None:
        """Get cached VLM evaluation by image hash + prompt hash."""
        key = f"{phash}:{prompt_hash}"
        return self.get("vlm", key)

    def set_vlm(self, phash: str, prompt_hash: str, result: dict) -> None:
        """Cache VLM evaluation result."""
        key = f"{phash}:{prompt_hash}"
        self.set("vlm", key, result, ttl=VLM_TTL)

    def get_ebay(self, query_hash: str) -> dict | None:
        """Get cached eBay lookup result by normalized query hash."""
        return self.get("ebay", query_hash)

    def set_ebay(self, query_hash: str, result: dict) -> None:
        """Cache eBay lookup result."""
        self.set("ebay", query_hash, result, ttl=EBAY_TTL)

    def get_search(self, query_hash: str) -> str | None:
        """Get cached web search result by query hash."""
        return self.get("search", query_hash)

    def set_search(self, query_hash: str, result: str) -> None:
        """Cache web search result."""
        self.set("search", query_hash, result, ttl=SEARCH_TTL)

    @property
    def stats(self) -> dict[str, int]:
        """Return cache statistics."""
        return {
            "size": len(self._cache),
            "volume_bytes": self._cache.volume(),
        }

    def close(self) -> None:
        """Close the cache database."""
        self._cache.close()
