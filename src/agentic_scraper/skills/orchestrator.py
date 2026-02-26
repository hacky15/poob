"""SmartDealRadar - LLM-orchestrated deal evaluation using skill tools."""

from __future__ import annotations

from typing import Any, Callable, Awaitable

from agentic_scraper.skills.models import (
    CategoryEstimate,
    ItemIdentification,
    PriceLookupResult,
)
from agentic_scraper.storage.models import Deal, DealScore, Listing
from agentic_scraper.utils.logging import get_logger

log = get_logger("skills.orchestrator")

# Score ranking for comparison
_SCORE_RANK: dict[DealScore, int] = {
    DealScore.UNKNOWN: 0,
    DealScore.FAIR: 1,
    DealScore.GOOD: 2,
    DealScore.GREAT: 3,
    DealScore.INCREDIBLE: 4,
}

# Discount thresholds for deal scoring
_DISCOUNT_THRESHOLDS = [
    (60.0, DealScore.INCREDIBLE),
    (40.0, DealScore.GREAT),
    (20.0, DealScore.GOOD),
    (0.0, DealScore.FAIR),
]


class SmartDealRadar:
    """LLM-orchestrated deal evaluation using skill tools.

    The SmartDealRadar follows a deterministic evaluation flow:
    1. Identify the item from text (and optionally images).
    2. Look up real market prices (eBay sold, retail, category estimate).
    3. Compare listing price to market price and score the deal.

    Args:
        identify_tool: Callable for text-based item identification.
        visual_identify_tool: Callable for image-based identification.
        ebay_lookup_tool: Callable for eBay sold price lookup.
        retail_lookup_tool: Callable for retail/MSRP lookup.
        category_estimate_tool: Callable for category-level pricing.
        min_score: Minimum DealScore to return (lower scores return None).
        ebay_min_samples: Minimum eBay samples for confident pricing.
        scam_threshold_pct: Discount % that triggers scam warning.
    """

    def __init__(
        self,
        *,
        identify_tool: Callable[..., Awaitable[ItemIdentification]],
        visual_identify_tool: Callable[..., Awaitable[ItemIdentification]],
        ebay_lookup_tool: Callable[..., Awaitable[PriceLookupResult]],
        retail_lookup_tool: Callable[..., Awaitable[PriceLookupResult]],
        category_estimate_tool: Callable[..., Awaitable[CategoryEstimate]],
        min_score: DealScore = DealScore.GOOD,
        ebay_min_samples: int = 3,
        scam_threshold_pct: float = 80.0,
    ) -> None:
        self._identify = identify_tool
        self._visual_identify = visual_identify_tool
        self._ebay_lookup = ebay_lookup_tool
        self._retail_lookup = retail_lookup_tool
        self._category_estimate = category_estimate_tool
        self._min_score = min_score
        self._ebay_min_samples = ebay_min_samples
        self._scam_threshold_pct = scam_threshold_pct

    async def evaluate(self, listing: Listing) -> Deal | None:
        """Evaluate a single listing using the skill pipeline.

        Args:
            listing: The marketplace listing to evaluate.

        Returns:
            A Deal object if the listing is a deal, None otherwise.
        """
        if not listing.price or listing.price <= 0:
            return None

        # Step 1: Identify the item
        identification = await self._step_identify(listing)

        # Step 2: Look up market prices
        market_price, price_source, price_confidence = await self._step_price_lookup(
            identification, listing
        )

        if market_price <= 0:
            log.info(
                "No price data found, skipping listing",
                listing_id=listing.id,
                title=listing.title,
            )
            return None

        # Step 3: Score the deal
        return self._step_score(
            listing, identification, market_price, price_source, price_confidence
        )

    async def _step_identify(self, listing: Listing) -> ItemIdentification:
        """Step 1: Identify the item from text, falling back to visual."""
        try:
            identification = await self._identify(
                listing.title, listing.description or ""
            )
        except Exception as exc:
            log.warning("Identification failed", error=str(exc), title=listing.title)
            identification = ItemIdentification(
                item_name=listing.title, confidence=0.0, needs_visual=True
            )

        # Use visual identification if text is uncertain and images are available
        if (
            (identification.confidence < 0.5 or identification.needs_visual)
            and listing.image_urls
        ):
            try:
                visual_id = await self._visual_identify(
                    image_urls=listing.image_urls
                )
                if visual_id.confidence > identification.confidence:
                    log.info(
                        "Visual ID improved confidence",
                        text_conf=identification.confidence,
                        visual_conf=visual_id.confidence,
                    )
                    identification = visual_id
            except Exception as exc:
                log.warning("Visual identification failed", error=str(exc))

        return identification

    async def _step_price_lookup(
        self, identification: ItemIdentification, listing: Listing
    ) -> tuple[float, str, float]:
        """Step 2: Look up market prices using available tools.

        Returns:
            Tuple of (market_price, price_source, confidence).
        """
        # Build search query from identification
        search_query = identification.item_name
        if identification.brand and identification.brand not in search_query:
            search_query = f"{identification.brand} {search_query}"

        # Try eBay sold prices first
        try:
            ebay_result = await self._ebay_lookup(
                search_query, condition=identification.condition
            )
        except Exception as exc:
            log.warning("eBay lookup failed", error=str(exc))
            ebay_result = PriceLookupResult(sample_count=0, confidence=0.0)

        if ebay_result.sample_count >= self._ebay_min_samples:
            return (
                ebay_result.median_price,
                "ebay_sold",
                ebay_result.confidence,
            )

        # Try retail price if eBay data insufficient
        try:
            retail_result = await self._retail_lookup(
                identification.item_name,
                brand=identification.brand,
                model=identification.model,
            )
        except Exception as exc:
            log.warning("Retail lookup failed", error=str(exc))
            retail_result = PriceLookupResult(sample_count=0, confidence=0.0)

        # Use eBay if it has some data, even if below min samples
        if ebay_result.sample_count > 0 and ebay_result.confidence > retail_result.confidence:
            return (
                ebay_result.median_price,
                "ebay_sold",
                ebay_result.confidence,
            )

        if retail_result.sample_count > 0 and retail_result.confidence > 0:
            # Retail is new price; apply used discount (30-50% for used items)
            used_factor = 0.6  # Assume ~40% depreciation from retail
            if identification.condition == "like new":
                used_factor = 0.75
            elif identification.condition == "parts":
                used_factor = 0.2
            elif identification.condition == "new":
                used_factor = 0.95

            adjusted_price = retail_result.median_price * used_factor
            return (adjusted_price, "retail", retail_result.confidence * 0.8)

        # Last resort: category estimate
        try:
            category_result = await self._category_estimate(
                identification.category or "general",
                condition=identification.condition,
                description_hints=listing.description or "",
            )
        except Exception as exc:
            log.warning("Category estimate failed", error=str(exc))
            return (0.0, "", 0.0)

        if category_result.typical_price > 0:
            return (
                category_result.typical_price,
                "category_estimate",
                category_result.confidence,
            )

        return (0.0, "", 0.0)

    def _step_score(
        self,
        listing: Listing,
        identification: ItemIdentification,
        market_price: float,
        price_source: str,
        price_confidence: float,
    ) -> Deal | None:
        """Step 3: Compare listing price to market price and score the deal."""
        listing_price = listing.price or 0
        if listing_price <= 0 or market_price <= 0:
            return None

        discount_pct = ((market_price - listing_price) / market_price) * 100.0
        discount_pct = max(discount_pct, 0.0)

        # Determine deal score from discount percentage
        score = DealScore.UNKNOWN
        for threshold, deal_score in _DISCOUNT_THRESHOLDS:
            if discount_pct >= threshold:
                score = deal_score
                break

        # Check minimum score threshold
        if _SCORE_RANK.get(score, 0) < _SCORE_RANK.get(self._min_score, 0):
            return None

        # Build reasoning
        red_flags: list[str] = []

        if discount_pct >= self._scam_threshold_pct:
            red_flags.append(
                f"Very high discount ({discount_pct:.0f}%) - verify in person"
            )

        if identification.confidence < 0.5:
            red_flags.append("Item identification uncertain")

        if price_confidence < 0.5:
            red_flags.append(f"Price data confidence low ({price_source})")

        reasoning_parts = [
            f"{identification.item_name} typically sells for ${market_price:.0f} ({price_source}).",
            f"Listed at ${listing_price:.0f} ({discount_pct:.0f}% below market).",
        ]
        if red_flags:
            reasoning_parts.append(f"Flags: {', '.join(red_flags)}.")

        reasoning = " ".join(reasoning_parts)

        log.info(
            "Deal scored",
            listing_id=listing.id,
            item=identification.item_name,
            score=score.value,
            discount_pct=round(discount_pct, 1),
            market_price=market_price,
            listing_price=listing_price,
        )

        return Deal(
            listing_id=listing.id or "",
            score=score,
            estimated_market_price=market_price,
            discount_pct=round(discount_pct, 1),
            llm_reasoning=reasoning,
        )
