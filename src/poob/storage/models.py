"""Data models used throughout the application."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


class DealScore(Enum):
    """How good of a deal this is."""

    UNKNOWN = "unknown"
    FAIR = "fair"
    GOOD = "good"
    GREAT = "great"
    INCREDIBLE = "incredible"


@dataclass
class Listing:
    """A single marketplace listing."""

    id: str | None = None
    site: str = ""
    external_id: str = ""
    title: str = ""
    price: float | None = None
    currency: str = "USD"
    description: str = ""
    location: str = ""
    seller_name: str = ""
    image_urls: list[str] = field(default_factory=list)
    listing_url: str = ""
    posted_at: datetime | None = None
    scraped_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    raw_data: dict = field(default_factory=dict)
    is_sponsored: bool = False
    evaluated: bool = False


@dataclass
class WatchItem:
    """A user's interest - things to look out for in the marketplace."""

    id: str | None = None
    interest: str = ""
    max_price: float | None = None
    location: str | None = None
    radius_miles: int | None = None
    category: str | None = None
    sites: list[str] = field(default_factory=list)
    is_active: bool = True
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    discord_user_id: str = ""
    discord_channel_id: str = ""
    notification_threshold: str = "good"  # "all" | "good" | "great" | "incredible" | "free"
    notes: str = ""  # User preference filters (e.g., "not metal", "no IKEA")
    effort: str = "normal"  # "normal" | "max" — controls evaluation thoroughness
    # "max" effort: all listing images sent to VLM, best provider forced,
    # unbranded cap bypassed, OCR text extracted from every image.
    search_configs: list[dict] = field(default_factory=list)
    # Per-search configs: [{"location": "madison", "radius_miles": 20, "condition": "good", ...}]
    # When empty, uses global patrol defaults.


@dataclass
class DealFeedback:
    """User feedback on a deal notification via Discord reactions.

    Enriched fields store full context for the learning pipeline:
    feedback + listing data + VLM output = training examples for
    dynamic few-shot, confidence calibration, and DSPy optimization.
    """

    id: str | None = None
    deal_id: str = ""
    discord_user_id: str = ""
    feedback_type: str = ""  # "claimed" | "overpriced" | "scam" | "not_interested"
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    # Enriched context for learning pipeline
    listing_title: str = ""
    listing_price: float = 0.0
    vlm_output: dict = field(default_factory=dict)
    deal_quality: str = ""
    estimated_value: float = 0.0
    provider_used: str = ""
    enrichment_data: dict = field(default_factory=dict)


@dataclass
class ExclusionItem:
    """An item/keyword to exclude from general search results and deal notifications."""

    id: str | None = None
    keyword: str = ""
    discord_user_id: str = ""
    is_active: bool = True
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class Deal:
    """A listing matched against a watch or flagged by deal radar."""

    id: str | None = None
    listing_id: str = ""
    watch_item_id: str | None = None
    score: DealScore = DealScore.UNKNOWN
    estimated_market_price: float | None = None
    discount_pct: float | None = None
    llm_reasoning: str = ""
    provenance_json: str = ""
    notified: bool = False
    notified_at: datetime | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class ScanLog:
    """Audit log for each scan cycle."""

    id: str | None = None
    site: str = ""
    category: str = ""
    listings_found: int = 0
    deals_found: int = 0
    errors: list[str] = field(default_factory=list)
    duration_seconds: float = 0.0
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: datetime | None = None
