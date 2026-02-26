"""Data models for skill inputs and outputs."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ItemIdentification:
    """Result of identifying an item from text or images.

    Attributes:
        item_name: Best guess at specific product name.
        brand: Detected brand, if any.
        model: Detected model number/name, if any.
        category: Hierarchical category (e.g. "electronics/gaming/console").
        condition: Detected condition (new, like new, good, fair, parts).
        confidence: How confident the identification is (0.0-1.0).
        needs_visual: True if text is too vague for confident identification.
    """

    item_name: str = ""
    brand: str | None = None
    model: str | None = None
    category: str = ""
    condition: str | None = None
    confidence: float = 0.0
    needs_visual: bool = False


@dataclass(frozen=True)
class PriceLookupResult:
    """Result of a price lookup from any source.

    Attributes:
        median_price: Median of found prices.
        average_price: Mean of found prices.
        min_price: Lowest price found.
        max_price: Highest price found.
        sample_count: Number of price points found.
        source: Where the data came from (ebay_sold, retail, etc.).
        search_query: The query used for the lookup.
        confidence: How reliable this data is (0.0-1.0).
    """

    median_price: float = 0.0
    average_price: float = 0.0
    min_price: float = 0.0
    max_price: float = 0.0
    sample_count: int = 0
    source: str = ""
    search_query: str = ""
    confidence: float = 0.0


@dataclass(frozen=True)
class CategoryEstimate:
    """Price estimate based on item category rather than exact identification.

    Attributes:
        low_price: Low end of typical price range.
        high_price: High end of typical price range.
        typical_price: Most common price point.
        source: Always "category_estimate".
        confidence: Always lower than data-backed lookups.
    """

    low_price: float = 0.0
    high_price: float = 0.0
    typical_price: float = 0.0
    source: str = "category_estimate"
    confidence: float = 0.0


@dataclass
class DealEvaluation:
    """Final evaluation result from the SmartDealRadar orchestrator.

    Attributes:
        item_identified: What the item was identified as.
        identification_confidence: How sure we are about the item ID.
        market_price: Best estimate of market value.
        price_source: Where the price data came from.
        deal_score: How good the deal is.
        discount_pct: Percentage below market price.
        reasoning: Human-readable explanation.
        red_flags: List of concerns or warnings.
    """

    item_identified: str = ""
    identification_confidence: float = 0.0
    market_price: float = 0.0
    price_source: str = ""
    deal_score: str = "fair"
    discount_pct: float = 0.0
    reasoning: str = ""
    red_flags: list[str] = field(default_factory=list)
