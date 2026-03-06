"""Visual enrichment via reverse image search APIs.

Tiered cascade:
  Tier 1: Google Cloud Vision Web Detection (1,000/mo free)
  Tier 2: SerpAPI Google Lens products (250/mo free)
  Tier 3: No enrichment (VLM works without external context)

When quotas exhaust, cascades gracefully to the next tier.  Never pays.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import httpx

from agentic_scraper.skills.models import VisualEnrichment
from agentic_scraper.utils.logging import get_logger

if TYPE_CHECKING:
    from agentic_scraper.config import AppConfig

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


class _MonthlyQuota:
    """Simple in-memory monthly quota tracker.  Resets on month change."""

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
    """

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._vision_quota = _MonthlyQuota(config.google_cloud_vision_monthly_quota)
        self._serpapi_quota = _MonthlyQuota(config.serpapi_monthly_quota)
        # Session-level circuit breakers: disable tier after hard failures
        # (403 = API not enabled, 429 = rate limited)
        self._vision_disabled = False
        self._serpapi_disabled = False
        # Locks serialize the first attempt per tier so concurrent requests
        # don't all fail before the circuit breaker trips.
        self._vision_lock = asyncio.Lock()
        self._serpapi_lock = asyncio.Lock()

    async def enrich(self, image_url: str) -> VisualEnrichment:
        """Enrich a listing image through the tiered API cascade.

        Args:
            image_url: URL of the listing's primary image.

        Returns:
            VisualEnrichment with product data, or empty tier-3 result.
        """
        # Tier 1: Google Cloud Vision Web Detection
        # Lock serializes concurrent requests so only the first one hits the API;
        # subsequent requests see _vision_disabled=True and skip immediately.
        if (
            self._config.google_cloud_vision_enabled
            and self._config.google_cloud_vision_api_key
            and self._vision_quota.has_remaining()
            and not self._vision_disabled
        ):
            async with self._vision_lock:
                if not self._vision_disabled:
                    try:
                        result = await self._google_vision_enrich(image_url)
                        if result.enriched_product_name:
                            self._vision_quota.increment()
                            return result
                        self._vision_quota.increment()
                    except httpx.HTTPStatusError as exc:
                        if exc.response.status_code in (403, 401):
                            self._vision_disabled = True
                            log.warning(
                                "Vision API disabled for session (auth/permission error)",
                                status=exc.response.status_code,
                            )
                        elif exc.response.status_code == 429:
                            self._vision_disabled = True
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
            and self._serpapi_quota.has_remaining()
            and not self._serpapi_disabled
        ):
            async with self._serpapi_lock:
                if not self._serpapi_disabled:
                    try:
                        result = await self._serpapi_lens_enrich(image_url)
                        if result.enriched_product_name:
                            self._serpapi_quota.increment()
                            return result
                        self._serpapi_quota.increment()
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

    # ------------------------------------------------------------------
    # Tier 1: Google Cloud Vision Web Detection + OCR
    # ------------------------------------------------------------------

    async def _google_vision_enrich(self, image_url: str) -> VisualEnrichment:
        """Call Google Cloud Vision API for web detection and OCR.

        Uses the REST API directly (no heavy proto dependency at runtime).

        Args:
            image_url: Image to analyze.

        Returns:
            VisualEnrichment with tier=1 data.
        """
        api_key = self._config.google_cloud_vision_api_key
        url = (
            "https://vision.googleapis.com/v1/images:annotate"
            f"?key={api_key}"
        )

        body = {
            "requests": [
                {
                    "image": {"source": {"imageUri": image_url}},
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
                enriched_product_name = _clean_product_name(raw)

        web_entities = [
            {"name": e.get("description", ""), "score": e.get("score", 0.0)}
            for e in web_entities_raw
            if e.get("description")
        ]

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
        return max(
            0,
            self._config.google_cloud_vision_monthly_quota - self._vision_quota.count,
        )

    @property
    def serpapi_quota_remaining(self) -> int:
        """Remaining SerpAPI calls this month."""
        return max(
            0,
            self._config.serpapi_monthly_quota - self._serpapi_quota.count,
        )
