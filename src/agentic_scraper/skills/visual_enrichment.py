"""Visual enrichment via reverse image search APIs.

Tiered cascade:
  Tier 1: Google Cloud Vision Web Detection (1,000/mo free)
  Tier 2: SerpAPI Google Lens products (250/mo free)
  Tier 3: No enrichment (VLM works without external context)

When quotas exhaust, cascades gracefully to the next tier.  Never pays.

Enhancements (Phase 1):
  - Persistent quota tracking via pyrate-limiter (survives restarts)
  - Perceptual hash caching via diskcache (deduplicates API calls)
  - Circuit breakers via aiobreaker (fast-fail on broken services)
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import httpx

from agentic_scraper.skills.models import VisualEnrichment
from agentic_scraper.utils.logging import get_logger

from aiobreaker import CircuitBreakerError

if TYPE_CHECKING:
    from agentic_scraper.cache.hash_cache import ResultCache
    from agentic_scraper.config import AppConfig
    from agentic_scraper.quota.persistent_limiter import PersistentKV, PersistentQuota
    from agentic_scraper.resilience.circuit_breaker import ServiceCircuitBreaker

log = get_logger("skills.visual_enrichment")


import re as _re

# Source prefixes that SerpAPI/Google Lens prepend to product titles.
_SOURCE_PREFIXES = _re.compile(
    r"^(Amazon\.com:\s*|eBay:\s*|Walmart\.com:\s*|Target:\s*|Best Buy:\s*"
    r"|Etsy:\s*|Wayfair:\s*|Home Depot:\s*|Lowe's:\s*)",
    _re.IGNORECASE,
)


def _clean_product_name(name: str) -> str:
    """Strip e-commerce source prefixes and truncate overly long product names.

    SerpAPI Google Lens returns titles like "Amazon.com: TCL 85\" Class 4-Series..."
    which cause Tavily 432 errors when used as search queries.

    Args:
        name: Raw product name from SerpAPI or Vision API.

    Returns:
        Cleaned product name suitable for eBay search queries.
    """
    name = _SOURCE_PREFIXES.sub("", name).strip()
    # Truncate at 80 chars to avoid Tavily query length issues
    if len(name) > 80:
        # Try to break at a word boundary
        truncated = name[:80].rsplit(" ", 1)[0]
        name = truncated if len(truncated) > 40 else name[:80]
    return name


# Generic visual descriptions that Google Vision bestGuessLabels returns instead
# of real product names.  These produce useless eBay searches.
_GENERIC_LABELS = frozenset({
    "appliance", "art", "barbecue", "black", "blue", "board", "bowl", "box",
    "brown", "cabinet", "ceramic", "chair", "circle", "clothing", "design",
    "desk", "device", "door", "electronics", "engine", "equipment", "fabric",
    "fence", "flashlight", "floor", "frame", "furniture", "gadget", "glass",
    "green",
    "grey", "grill", "handwriting", "hardware", "image", "instrument",
    "item", "kitchen", "label", "light", "line", "machine", "major",
    "material", "metal", "monitor", "motor", "newsprint", "number", "object",
    "orange", "outdoor",
    "paper", "part", "pattern", "picture", "pink", "plank", "plant",
    "plastic", "plywood", "poster", "product", "purple", "rack", "rectangle",
    "red", "rock", "room", "rubble",
    "shape", "shelf", "speaker", "square", "stairs", "staircase", "stand",
    "storage", "table", "text", "texture", "thing", "tool", "tread",
    "triangle", "turf", "vehicle",
    "wall", "watch", "white", "wood", "yellow",
})


# Non-product filler words that Vision API picks up from website names,
# business names, and location references.  Combined with _GENERIC_LABELS
# to detect word-salad "product names" like "Barbecue Jean Sport Aviation Center".
_FILLER_WORDS = frozenset({
    "aviation", "cabbage", "center", "centre", "club", "company", "corp",
    "county", "delivery", "depot", "east", "farm", "fence", "food", "group",
    "home", "house", "inc", "jean", "llc", "market", "north", "onion",
    "park", "place", "plaza", "rail", "shop", "south", "split", "sport",
    "sports", "store", "supply", "west",
})


def _is_useful_product_name(name: str) -> bool:
    """Check if a Vision API product name is specific enough for eBay search.

    Rejects generic visual descriptions, random word salads, and names where
    most words are generic/filler terms.

    Args:
        name: Cleaned product name.

    Returns:
        True if the name is specific enough to be useful.
    """
    if not name:
        return False
    # Split on spaces and hyphens to handle "split-rail fence" → ["split", "rail", "fence"]
    words = _re.split(r"[\s\-]+", name.lower())
    words = [w for w in words if w]
    if not words:
        return False
    # Single-word names that are generic visual terms → useless
    if len(words) == 1 and words[0] in _GENERIC_LABELS:
        return False
    # Two-word names where both are short/generic → useless (e.g. "F net")
    if len(words) <= 2 and all(len(w) <= 3 for w in words):
        return False
    # Two-word labels: reject if EITHER word is in the generic set.
    # "analog watch", "standing desk", "artificial turf" are NOT real
    # product identifications — they're category guesses.
    if len(words) == 2:
        noise = _GENERIC_LABELS | _FILLER_WORDS
        if any(w in noise for w in words):
            return False
    # Multi-word labels (3+): count how many words are generic, filler, or very short
    # "Barbecue Jean Sport Aviation Center" → 5/5 non-product words → reject
    if len(words) >= 3:
        noise = _GENERIC_LABELS | _FILLER_WORDS
        noise_count = sum(1 for w in words if w in noise or len(w) <= 2)
        if noise_count >= len(words) * 0.5:
            return False
    # Names with no digits and no word ≥6 chars are unlikely to be real products
    # (real products have brand names or model numbers)
    has_digit = any(c.isdigit() for c in name)
    has_long_word = any(len(w) >= 6 for w in words)
    if len(words) >= 3 and not has_digit and not has_long_word:
        return False
    return True


class _MonthlyQuota:
    """Simple in-memory monthly quota tracker.  Resets on month change.

    DEPRECATED: Prefer PersistentQuota from quota.persistent_limiter.
    Kept as fallback when persistent quotas are not injected.
    """

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._count = 0
        self._month: int = datetime.now(timezone.utc).month

    def _maybe_reset(self) -> None:
        now_month = datetime.now(timezone.utc).month
        if now_month != self._month:
            self._month = now_month
            self._count = 0

    def has_remaining(self) -> bool:
        self._maybe_reset()
        return self._count < self._limit

    def try_acquire(self) -> bool:
        """Consume one unit if available. Returns True on success."""
        self._maybe_reset()
        if self._count < self._limit:
            self._count += 1
            return True
        return False

    def increment(self) -> None:
        self._maybe_reset()
        self._count += 1

    @property
    def count(self) -> int:
        self._maybe_reset()
        return self._count


class VisualEnrichmentService:
    """Tiered visual enrichment for nameless marketplace listings.

    Takes an image URL, cascades through free reverse-image-search APIs to
    identify the product, and returns enrichment data (product name, brand,
    model, retail prices, OCR text).

    Args:
        config: Application configuration.
        vision_quota: Persistent Vision API quota tracker (optional, falls back to in-memory).
        serpapi_quota: Persistent SerpAPI quota tracker (optional, falls back to in-memory).
        result_cache: Perceptual hash cache for deduplicating API calls (optional).
        vision_breaker: Circuit breaker for Vision API calls (optional).
        serpapi_breaker: Circuit breaker for SerpAPI calls (optional).
    """

    def __init__(
        self,
        config: AppConfig,
        *,
        vision_quota: PersistentQuota | None = None,
        serpapi_quota: PersistentQuota | None = None,
        result_cache: ResultCache | None = None,
        vision_breaker: ServiceCircuitBreaker | None = None,
        serpapi_breaker: ServiceCircuitBreaker | None = None,
        kv_store: PersistentKV | None = None,
    ) -> None:
        self._config = config
        # Use persistent quotas if provided, else fall back to in-memory
        self._vision_quota: PersistentQuota | _MonthlyQuota = (
            vision_quota
            if vision_quota is not None
            else _MonthlyQuota(config.google_cloud_vision_monthly_quota)
        )
        self._serpapi_quota: PersistentQuota | _MonthlyQuota = (
            serpapi_quota
            if serpapi_quota is not None
            else _MonthlyQuota(config.serpapi_monthly_quota)
        )
        # Perceptual hash cache — deduplicates Vision/SerpAPI calls on identical images
        self._cache = result_cache
        # Circuit breakers — fast-fail on broken services
        self._vision_breaker = vision_breaker
        self._serpapi_breaker = serpapi_breaker
        # Session-level circuit breakers: disable tier after hard failures
        # (403 = API not enabled, 429 = rate limited)
        self._serpapi_disabled = False
        # Locks serialize the first attempt per tier so concurrent requests
        # don't all fail before the circuit breaker trips.
        self._vision_lock = asyncio.Lock()
        self._serpapi_lock = asyncio.Lock()
        # Adaptive disabling: rolling window of Vision API cross-validation results.
        # If mismatch rate exceeds threshold over the window, auto-disable.
        # Rolling window catches intermittent hallucinations (e.g. 8/10 wrong)
        # that consecutive-only tracking misses when valid results are interspersed.
        self._kv = kv_store
        self._VISION_WINDOW_SIZE = 10
        self._VISION_MISMATCH_RATE_THRESHOLD = 0.7  # 70%+ mismatches → disable

        # Restore disabled flag and mismatch window from persistent storage so
        # a Vision API that was auto-disabled last session stays disabled.
        if self._kv:
            self._vision_disabled = self._kv.get_bool("vision_api_disabled", False)
            raw_window = self._kv.get("vision_mismatch_window")
            if raw_window:
                import json as _json
                try:
                    self._vision_results: list[bool] = _json.loads(raw_window)
                except Exception:
                    self._vision_results = []
            else:
                self._vision_results = []
            if self._vision_disabled:
                log.info(
                    "Vision API remains disabled from previous session",
                    window_size=len(self._vision_results),
                )
        else:
            self._vision_disabled = False
            self._vision_results = []

    async def enrich(self, image_url: str) -> VisualEnrichment:
        """Enrich a listing image through the tiered API cascade.

        Checks perceptual hash cache first to avoid re-querying APIs on
        visually identical images. Caches successful results.

        Args:
            image_url: URL of the listing's primary image.

        Returns:
            VisualEnrichment with product data, or empty tier-3 result.
        """
        # Download image once for both caching and API calls
        image_bytes: bytes | None = None
        image_phash: str | None = None
        try:
            async with httpx.AsyncClient(timeout=10, follow_redirects=True) as dl:
                img_resp = await dl.get(image_url)
                img_resp.raise_for_status()
                image_bytes = img_resp.content
        except Exception as exc:
            log.debug("Image download failed for cache", error=str(exc)[:100])

        # Compute perceptual hash for cache lookup
        if image_bytes and self._cache:
            from agentic_scraper.cache.hash_cache import compute_phash

            image_phash = compute_phash(image_bytes)

            # Check cache for Vision API result
            cached_vision = self._cache.get_vision(image_phash)
            if cached_vision:
                log.info("cache.hit.vision", phash=image_phash[:12])
                return self._dict_to_enrichment(cached_vision, tier=1)

            # Check cache for SerpAPI result
            cached_serpapi = self._cache.get_serpapi(image_phash)
            if cached_serpapi:
                log.info("cache.hit.serpapi", phash=image_phash[:12])
                return self._dict_to_enrichment(cached_serpapi, tier=2)

        # Tier 1: Google Cloud Vision Web Detection
        if (
            self._config.google_cloud_vision_enabled
            and self._config.google_cloud_vision_api_key
            and not self._vision_disabled
            and not (self._vision_breaker and self._vision_breaker.is_open)
        ):
            async with self._vision_lock:
                # has_remaining() MUST be inside the lock to prevent race
                # conditions where concurrent tasks both pass the check,
                # then both make API calls exceeding the monthly quota.
                if not self._vision_disabled and self._vision_quota.has_remaining():
                    try:
                        # Route through circuit breaker so failures are tracked
                        if self._vision_breaker:
                            result = await self._vision_breaker.call(
                                self._google_vision_enrich,
                                image_url,
                                image_bytes=image_bytes,
                            )
                        else:
                            result = await self._google_vision_enrich(
                                image_url, image_bytes=image_bytes
                            )
                        self._vision_quota.try_acquire()
                        if result.enriched_product_name:
                            # Cache the result
                            if self._cache and image_phash:
                                self._cache.set_vision(
                                    image_phash,
                                    self._enrichment_to_dict(result),
                                )
                            return result
                    except CircuitBreakerError:
                        log.warning(
                            "Vision API circuit breaker open, cascading to Tier 2"
                        )
                    except httpx.HTTPStatusError as exc:
                        if exc.response.status_code in (403, 401):
                            self._vision_disabled = True
                            if self._kv:
                                self._kv.set_bool("vision_api_disabled", True)
                            log.warning(
                                "Vision API disabled for session (auth/permission error)",
                                status=exc.response.status_code,
                                body=exc.response.text[:300],
                            )
                        elif exc.response.status_code == 429:
                            self._vision_disabled = True
                            if self._kv:
                                self._kv.set_bool("vision_api_disabled", True)
                            log.warning(
                                "Vision API disabled for session (rate limited)"
                            )
                        else:
                            log.warning(
                                "Vision API failed, cascading to Tier 2",
                                error=str(exc)[:200],
                            )
                    except Exception as exc:
                        log.warning(
                            "Vision API failed, cascading to Tier 2",
                            error=str(exc)[:200],
                        )

        # Tier 2: SerpAPI Google Lens
        if (
            self._config.serpapi_enabled
            and self._config.serpapi_api_key
            and not self._serpapi_disabled
            and not (self._serpapi_breaker and self._serpapi_breaker.is_open)
        ):
            async with self._serpapi_lock:
                if not self._serpapi_disabled and self._serpapi_quota.has_remaining():
                    try:
                        # Route through circuit breaker so failures are tracked
                        if self._serpapi_breaker:
                            result = await self._serpapi_breaker.call(
                                self._serpapi_lens_enrich, image_url
                            )
                        else:
                            result = await self._serpapi_lens_enrich(image_url)
                        self._serpapi_quota.try_acquire()
                        if result.enriched_product_name:
                            # Cache the result
                            if self._cache and image_phash:
                                self._cache.set_serpapi(
                                    image_phash,
                                    self._enrichment_to_dict(result),
                                )
                            return result
                    except CircuitBreakerError:
                        log.warning(
                            "SerpAPI circuit breaker open, falling to Tier 3"
                        )
                    except httpx.HTTPStatusError as exc:
                        if exc.response.status_code == 429:
                            self._serpapi_disabled = True
                            log.warning(
                                "SerpAPI disabled for session (rate limited)"
                            )
                        elif exc.response.status_code in (403, 401):
                            self._serpapi_disabled = True
                            log.warning(
                                "SerpAPI disabled for session (auth/permission error)",
                                status=exc.response.status_code,
                            )
                        else:
                            log.warning(
                                "SerpAPI Google Lens failed, falling to Tier 3",
                                error=str(exc)[:200],
                            )
                    except Exception as exc:
                        log.warning(
                            "SerpAPI Google Lens failed, falling to Tier 3",
                            error=str(exc)[:200],
                        )

        # Tier 3: No enrichment — VLM will work without external context
        log.debug("No enrichment available (Tier 3)", image_url=image_url[:80])
        return VisualEnrichment(enrichment_tier=3, enrichment_confidence=0.0)

    def report_vision_result(self, *, matched: bool) -> None:
        """Report whether the Vision API result matched the listing (cross-validation).

        Called by the orchestrator after ``_build_validated_ebay_query`` compares
        the Vision API product name against the listing title. Uses a rolling
        window: if the mismatch rate exceeds 70% over the last 10 results,
        auto-disables Vision API for the session to stop burning quota.

        Args:
            matched: True if enrichment overlapped with the listing title.
        """
        self._vision_results.append(matched)
        # Keep only the last N results
        if len(self._vision_results) > self._VISION_WINDOW_SIZE:
            self._vision_results = self._vision_results[-self._VISION_WINDOW_SIZE:]

        # Persist rolling window so it survives restarts
        if self._kv:
            import json as _json
            self._kv.set("vision_mismatch_window", _json.dumps(self._vision_results))

        # Only check once we have enough data
        if len(self._vision_results) >= self._VISION_WINDOW_SIZE and not self._vision_disabled:
            mismatches = sum(1 for r in self._vision_results if not r)
            mismatch_rate = mismatches / len(self._vision_results)
            if mismatch_rate >= self._VISION_MISMATCH_RATE_THRESHOLD:
                self._vision_disabled = True
                if self._kv:
                    self._kv.set_bool("vision_api_disabled", True)
                log.warning(
                    "Vision API auto-disabled (high hallucination rate)",
                    mismatch_rate=round(mismatch_rate, 2),
                    mismatches=mismatches,
                    window=len(self._vision_results),
                )

    # ------------------------------------------------------------------
    # Tier 1: Google Cloud Vision Web Detection + OCR
    # ------------------------------------------------------------------

    async def _google_vision_enrich(
        self, image_url: str, *, image_bytes: bytes | None = None
    ) -> VisualEnrichment:
        """Call Google Cloud Vision API for web detection and OCR.

        Downloads the image first and sends base64-encoded bytes, because
        Facebook's image CDN blocks Google's servers from fetching URLs
        directly (returns "We're not allowed to access the URL").

        Args:
            image_url: Image to analyze.
            image_bytes: Pre-downloaded image bytes (skips re-download).

        Returns:
            VisualEnrichment with tier=1 data.
        """
        import base64

        api_key = self._config.google_cloud_vision_api_key
        url = (
            "https://vision.googleapis.com/v1/images:annotate"
            f"?key={api_key}"
        )

        # Use pre-downloaded bytes if available, else download
        if image_bytes:
            image_b64 = base64.b64encode(image_bytes).decode("ascii")
        else:
            try:
                async with httpx.AsyncClient(timeout=10, follow_redirects=True) as dl_client:
                    img_resp = await dl_client.get(image_url)
                    img_resp.raise_for_status()
                    image_b64 = base64.b64encode(img_resp.content).decode("ascii")
            except Exception as exc:
                log.warning(
                    "Failed to download image for Vision API",
                    url=image_url[:80],
                    error=str(exc)[:100],
                )
                return VisualEnrichment(enrichment_tier=1, enrichment_confidence=0.0)

        body = {
            "requests": [
                {
                    "image": {"content": image_b64},
                    "features": [
                        {"type": "WEB_DETECTION", "maxResults": 10},
                        {"type": "TEXT_DETECTION", "maxResults": 5},
                    ],
                }
            ]
        }

        t0 = time.monotonic()
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(url, json=body)
            resp.raise_for_status()
            data = resp.json()

        elapsed = time.monotonic() - t0
        log.info("vision_api.response", elapsed_s=round(elapsed, 1))

        responses = data.get("responses", [{}])
        if not responses:
            return VisualEnrichment(enrichment_tier=1, enrichment_confidence=0.0)

        response = responses[0]

        # Check for errors in the response
        error = response.get("error")
        if error:
            log.warning("Vision API returned error", error=error.get("message", ""))
            return VisualEnrichment(enrichment_tier=1, enrichment_confidence=0.0)

        # Extract web detection results
        web = response.get("webDetection", {})
        best_guess = web.get("bestGuessLabels", [])
        web_entities_raw = web.get("webEntities", [])

        enriched_product_name: str | None = None
        if best_guess:
            raw = best_guess[0].get("label")
            if raw:
                cleaned = _clean_product_name(raw)
                if _is_useful_product_name(cleaned):
                    enriched_product_name = cleaned
                else:
                    log.debug(
                        "Vision API label too generic, ignoring",
                        label=raw,
                    )

        web_entities = [
            {"name": e.get("description", ""), "score": e.get("score", 0.0)}
            for e in web_entities_raw
            if e.get("description")
        ]

        # Fallback: if bestGuessLabel was generic, try the highest-scored
        # multi-word web entity (often more specific, e.g. "Sony WH-1000XM4")
        if not enriched_product_name and web_entities:
            for entity in web_entities:
                name = entity.get("name", "")
                if _is_useful_product_name(name) and len(name.split()) >= 2:
                    enriched_product_name = _clean_product_name(name)
                    log.debug(
                        "Using web entity as product name",
                        entity=enriched_product_name,
                    )
                    break

        # Extract OCR text
        ocr_text: str | None = None
        text_annotations = response.get("textAnnotations", [])
        if text_annotations:
            # First annotation is the full detected text
            ocr_text = text_annotations[0].get("description", "").strip()

        # Try to extract brand/model from web entities or OCR
        enriched_brand, enriched_model = self._extract_brand_model(
            web_entities, ocr_text, enriched_product_name
        )

        # Confidence based on web entity scores
        confidence = 0.0
        if web_entities:
            top_score = max(e.get("score", 0.0) for e in web_entities)
            confidence = min(top_score, 1.0)
        if enriched_product_name:
            confidence = max(confidence, 0.5)

        return VisualEnrichment(
            enriched_product_name=enriched_product_name,
            enriched_brand=enriched_brand,
            enriched_model=enriched_model,
            web_entities=web_entities,
            ocr_text=ocr_text,
            enrichment_tier=1,
            enrichment_confidence=round(confidence, 2),
        )

    # ------------------------------------------------------------------
    # Tier 2: SerpAPI Google Lens
    # ------------------------------------------------------------------

    async def _serpapi_lens_enrich(self, image_url: str) -> VisualEnrichment:
        """Call SerpAPI Google Lens for product identification.

        Args:
            image_url: Image to analyze.

        Returns:
            VisualEnrichment with tier=2 data.
        """
        params = {
            "engine": "google_lens",
            "url": image_url,
            "api_key": self._config.serpapi_api_key,
        }

        t0 = time.monotonic()
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(
                "https://serpapi.com/search.json", params=params
            )
            resp.raise_for_status()
            data = resp.json()

        elapsed = time.monotonic() - t0
        log.info("serpapi_lens.response", elapsed_s=round(elapsed, 1))

        # Extract visual matches and knowledge graph
        visual_matches = data.get("visual_matches", [])
        knowledge_graph = data.get("knowledge_graph", [])

        enriched_product_name: str | None = None
        enriched_brand: str | None = None
        enriched_model: str | None = None
        retail_prices: list[float] = []

        # Knowledge graph often has the best product identification
        if knowledge_graph:
            first = knowledge_graph[0] if isinstance(knowledge_graph, list) else knowledge_graph
            enriched_product_name = first.get("title")

        # Visual matches have product names and prices
        for match in visual_matches[:5]:
            title = match.get("title", "")
            if not enriched_product_name and title:
                enriched_product_name = title

            # Extract price if available
            price_info = match.get("price", {})
            if isinstance(price_info, dict):
                extracted = price_info.get("extracted_value")
                if extracted and isinstance(extracted, (int, float)):
                    retail_prices.append(float(extracted))
            elif isinstance(price_info, str):
                # Try to parse "$XX.XX" format
                import re
                price_match = re.search(r"\$?([\d,]+\.?\d*)", price_info)
                if price_match:
                    try:
                        retail_prices.append(
                            float(price_match.group(1).replace(",", ""))
                        )
                    except ValueError:
                        pass

        # Clean product names — SerpAPI often returns "Amazon.com: Product Name"
        if enriched_product_name:
            enriched_product_name = _clean_product_name(enriched_product_name)
            if not _is_useful_product_name(enriched_product_name):
                log.debug("SerpAPI label too generic, ignoring", label=enriched_product_name)
                enriched_product_name = None

        confidence = 0.5 if enriched_product_name else 0.0
        if retail_prices:
            confidence = min(confidence + 0.2, 1.0)

        return VisualEnrichment(
            enriched_product_name=enriched_product_name,
            enriched_brand=enriched_brand,
            enriched_model=enriched_model,
            retail_prices=retail_prices,
            enrichment_tier=2,
            enrichment_confidence=round(confidence, 2),
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_brand_model(
        web_entities: list[dict],
        ocr_text: str | None,
        product_name: str | None,
    ) -> tuple[str | None, str | None]:
        """Try to extract brand and model from enrichment data.

        Uses web entity labels and OCR text heuristics.

        Returns:
            (brand, model) tuple, either or both may be None.
        """
        brand: str | None = None
        model: str | None = None

        # Web entities with high scores often contain the brand
        for entity in web_entities[:5]:
            name = entity.get("name", "")
            score = entity.get("score", 0.0)
            if score >= 0.5 and len(name.split()) <= 2:
                # Short, high-confidence entity is likely a brand
                if not brand:
                    brand = name

        # If the product name starts with a word that matches a web entity, that's the brand
        if product_name and not brand:
            first_word = product_name.split()[0] if product_name.split() else ""
            for entity in web_entities[:5]:
                if entity.get("name", "").lower() == first_word.lower():
                    brand = entity["name"]
                    break

        return brand, model

    @property
    def vision_quota_remaining(self) -> int:
        """Remaining Google Cloud Vision API calls this month."""
        if hasattr(self._vision_quota, 'count'):
            return max(
                0,
                self._config.google_cloud_vision_monthly_quota - self._vision_quota.count,
            )
        return self._config.google_cloud_vision_monthly_quota  # Persistent quota doesn't expose count

    @property
    def serpapi_quota_remaining(self) -> int:
        """Remaining SerpAPI calls this month."""
        if hasattr(self._serpapi_quota, 'count'):
            return max(
                0,
                self._config.serpapi_monthly_quota - self._serpapi_quota.count,
            )
        return self._config.serpapi_monthly_quota

    @staticmethod
    def _enrichment_to_dict(enrichment: VisualEnrichment) -> dict:
        """Convert a VisualEnrichment to a cacheable dict."""
        return {
            "enriched_product_name": enrichment.enriched_product_name,
            "enriched_brand": enrichment.enriched_brand,
            "enriched_model": enrichment.enriched_model,
            "retail_prices": enrichment.retail_prices,
            "web_entities": enrichment.web_entities,
            "ocr_text": enrichment.ocr_text,
            "enrichment_tier": enrichment.enrichment_tier,
            "enrichment_confidence": enrichment.enrichment_confidence,
        }

    @staticmethod
    def _dict_to_enrichment(data: dict, tier: int = 1) -> VisualEnrichment:
        """Reconstruct a VisualEnrichment from a cached dict."""
        return VisualEnrichment(
            enriched_product_name=data.get("enriched_product_name"),
            enriched_brand=data.get("enriched_brand"),
            enriched_model=data.get("enriched_model"),
            retail_prices=data.get("retail_prices", []),
            web_entities=data.get("web_entities", []),
            ocr_text=data.get("ocr_text"),
            enrichment_tier=data.get("enrichment_tier", tier),
            enrichment_confidence=data.get("enrichment_confidence", 0.5),
        )
