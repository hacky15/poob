"""Data models for skill inputs and outputs."""

from __future__ import annotations

from dataclasses import dataclass, field

# Strings LLMs commonly return instead of JSON null
_NULL_STRINGS = frozenset({"null", "none", "n/a", "na", "unknown", ""})


def clean_optional(value: object) -> str | None:
    """Sanitize an LLM-returned optional string field.

    LLMs frequently return the literal string "null", "None", "N/A", etc.
    instead of JSON null.  This normalizes all of those to Python None.
    """
    if value is None:
        return None
    s = str(value).strip()
    if s.lower() in _NULL_STRINGS:
        return None
    return s


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
        urgency_signals: Seller urgency/motivation phrases detected in listing text.
    """

    item_name: str = ""
    brand: str | None = None
    model: str | None = None
    category: str = ""
    condition: str | None = None
    confidence: float = 0.0
    needs_visual: bool = False
    urgency_signals: tuple[str, ...] = ()


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


@dataclass(frozen=True)
class DealValidation:
    """Result of LLM deal validation — sanity check before notifying.

    Attributes:
        is_valid: Whether the deal is genuinely good.
        reasoning: Human-readable explanation of the judgment.
    """

    is_valid: bool = True
    reasoning: str = ""


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


# ---------------------------------------------------------------------------
# VLM pipeline models
# ---------------------------------------------------------------------------


@dataclass
class TriageResult:
    """Output from Stage 1 text triage for a single listing."""

    listing_id: str = ""
    investigate: bool = False
    reasoning: str = ""
    scam_signals: list[str] = field(default_factory=list)
    urgency_signals: list[str] = field(default_factory=list)
    misspelling_bonus: bool = False


@dataclass
class VisualEnrichment:
    """Output from Stage 2 visual enrichment for a single listing.

    Attributes:
        enriched_product_name: Product name identified by reverse image search.
        enriched_brand: Brand identified from image.
        enriched_model: Model identified from image.
        retail_prices: Retail prices found via Google Lens or web pages.
        web_entities: Entities and labels from Google Cloud Vision.
        ocr_text: Text extracted from listing images via OCR.
        enrichment_tier: Which enrichment source succeeded (1=Vision API, 2=SerpAPI, 3=none).
        enrichment_confidence: Confidence of the enrichment result.
    """

    enriched_product_name: str | None = None
    enriched_brand: str | None = None
    enriched_model: str | None = None
    retail_prices: list[float] = field(default_factory=list)
    web_entities: list[dict] = field(default_factory=list)
    ocr_text: str | None = None
    enrichment_tier: int = 3  # 1=Vision API, 2=SerpAPI, 3=none
    enrichment_confidence: float = 0.0


@dataclass
class VLMEvaluation:
    """Output from Stage 3 VLM deep evaluation.

    The VLM is the primary deal evaluator. It sees images, listing metadata,
    enrichment context, and comparable sold prices, then makes the deal call.

    Attributes:
        item_identified: Specific item with brand/model/specs from VLM.
        condition: Visual condition assessment (mint through parts).
        condition_notes: Specific observations from photos.
        depreciation_factor: Condition-based depreciation (0.1-1.0).
        estimated_value_low: Low end of estimated market value.
        estimated_value_mid: Mid (most likely) market value.
        estimated_value_high: High end of estimated market value.
        deal_quality: VLM's deal classification (pass/fair/good/great/incredible).
        confidence: VLM's confidence in its evaluation (0.0-1.0).
        reasoning: Brief explanation of the deal assessment.
        red_flags: Concerns identified by the VLM.
    """

    item_identified: str = ""
    condition: str = "unknown"
    condition_notes: str = ""
    depreciation_factor: float = 0.6
    estimated_value_low: float = 0.0
    estimated_value_mid: float = 0.0
    estimated_value_high: float = 0.0
    deal_quality: str = "pass"  # pass | fair | good | great | incredible
    # Whether this is a SPECIFIC, desirable, resaleable item worth notifying a
    # human about (vs. cheap commodity / consumable / non-item). Fail-open: the
    # default + omitted-field parse are True; only a hard False gates (public
    # path only). See docs/decisions/public-incredible-selectivity-floors.md.
    worth_attention: bool = True
    confidence: float = 0.0
    reasoning: str = ""
    red_flags: list[str] = field(default_factory=list)


@dataclass
class DealProvenance:
    """Lightweight provenance trail for a deal evaluation.

    Accumulates data as a listing flows through the pipeline stages.
    Serialized to JSON for storage and rendered in Discord embeds so
    the user can see exactly which models/services contributed to the
    deal score.
    """

    # Stage 1: Triage
    triage_bypass_reason: str = ""  # "watchlist" | "garbage_title" | ""
    scam_signals: list[str] = field(default_factory=list)
    urgency_signals: list[str] = field(default_factory=list)

    # Stage 2: Enrichment
    enrichment_tier: int = 3  # 1=Vision API, 2=Google Lens, 3=none
    enrichment_product: str = ""  # identified product name

    # Stage 2b: Price lookup
    price_source: str = ""  # "ebay_sold" | "retail" | ""
    price_confidence: float = 0.0
    price_sample_count: int = 0

    # Stage 3: VLM evaluation
    vlm_providers: list[str] = field(default_factory=list)
    vlm_agreement: float | None = None  # voting confidence
    vlm_raw_quality: str = ""  # VLM's original score BEFORE enforcement
    vlm_condition: str = ""  # mint|excellent|good|fair|poor|unknown
    vlm_condition_notes: str = ""
    vlm_confidence: float = 0.0
    vlm_item_identified: str = ""

    # Stage 3b: Web context
    web_search_used: bool = False
    web_search_provider: str = ""

    # Stage 4: Programmatic enforcement
    score_adjustments: list[str] = field(default_factory=list)
    final_score: str = ""

    def to_json(self) -> str:
        """Serialize for database storage."""
        import json
        from dataclasses import asdict

        return json.dumps(asdict(self), separators=(",", ":"))

    @classmethod
    def from_json(cls, raw: str) -> DealProvenance:
        """Deserialize from database storage. Gracefully handles empty/unknown fields."""
        import json

        if not raw:
            return cls()
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return cls()
        known = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**{k: v for k, v in data.items() if k in known})
