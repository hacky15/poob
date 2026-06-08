"""SmartDealRadar — VLM-driven deal evaluation pipeline.

Stage 1: Text triage (batch filter via cloud LLM)
Stage 2: Visual enrichment (Google Cloud Vision / SerpAPI) + comparable sales
Stage 3: VLM deep evaluation (Gemini/Groq/OpenRouter/Ollama cascade)

Supports two modes:
  - **Batch**: evaluate_batch() processes all listings through each stage sequentially.
  - **Streaming**: evaluate_streaming() uses asyncio.Queue between stages so the first
    deal can be found while other listings are still being triaged/enriched.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable, Coroutine
from typing import TYPE_CHECKING, Any, NamedTuple

from poob.skills.models import (
    DealProvenance,
    TriageResult,
    VisualEnrichment,
    VLMEvaluation,
)
from poob.storage.models import Deal, DealScore, Listing
from poob.utils.logging import get_logger

if TYPE_CHECKING:
    from poob.skills.ebay_lookup import EbayLookupTool
    from poob.skills.retail_lookup import RetailLookupTool
    from poob.skills.text_triage import TextTriageService
    from poob.skills.visual_enrichment import VisualEnrichmentService
    from poob.skills.vlm_evaluator import VLMDealEvaluator
    from poob.skills.web_search import SearchProviderCascade
    from poob.storage.models import WatchItem

# Sentinel value for queue termination
_DONE = object()

log = get_logger("skills.orchestrator")

# Map deal_quality strings from VLM → DealScore enum
_QUALITY_TO_SCORE: dict[str, DealScore] = {
    "pass": DealScore.FAIR,
    "fair": DealScore.FAIR,
    "good": DealScore.GOOD,
    "great": DealScore.GREAT,
    "incredible": DealScore.INCREDIBLE,
}

_SCORE_RANK: dict[DealScore, int] = {
    DealScore.UNKNOWN: 0,
    DealScore.FAIR: 1,
    DealScore.GOOD: 2,
    DealScore.GREAT: 3,
    DealScore.INCREDIBLE: 4,
}

# A watch item's notification_threshold maps to the deal-quality floor for THAT
# match. 'all'/'free' impose no quality floor — the user is watching a specific
# item and wants it surfaced regardless of discount ('free' is price-gated at
# notify, not here). Quality thresholds map to their DealScore. This is what
# lets a normally-priced wishlist match (scored FAIR) still notify when the user
# set 'all'. See docs/decisions/watchlist-honors-threshold-not-freshness.md.
_WATCH_THRESHOLD_TO_SCORE: dict[str, DealScore] = {
    "all": DealScore.UNKNOWN,
    "free": DealScore.UNKNOWN,
    "good": DealScore.GOOD,
    "great": DealScore.GREAT,
    "incredible": DealScore.INCREDIBLE,
}

# Titles that are Facebook UI artifacts, not real listing data.
# Named distinctly to avoid collision with _GARBAGE_TITLES used for triage bypass.
_UI_ARTIFACT_TITLES = frozenset({
    "see details", "more options", "seller details", "loading", "",
    "marketplace", "facebook marketplace",
})

# Words to strip when comparing enrichment vs listing title.
# Includes generic product category words that shouldn't drive overlap scores
# (e.g., "chair" matching between "Herman Miller Aeron Chair" and "Office chair").
_STOP_WORDS = frozenset({
    "a", "an", "the", "and", "or", "of", "for", "in", "on", "with", "to",
    "is", "it", "my", "i", "new", "used", "like", "very", "set", "lot",
    "great", "good", "nice", "free", "sale", "obo", "firm",
    # Generic product category words — too common to indicate a brand match
    "chair", "table", "desk", "shelf", "lamp", "bed", "couch", "sofa",
    "monitor", "printer", "phone", "tablet", "laptop", "computer",
    "machine", "maker", "tool", "saw", "drill", "mower",
    "dish", "bowl", "plate", "cup", "mug", "pan", "pot",
    "bag", "box", "case", "rack", "stand", "mount", "holder",
    "baby", "kids", "mini", "small", "large", "big", "old",
})


# Synonyms for cross-validation overlap — maps variant → canonical form.
# When Vision API says "television" and title says "tv", these should match.
_CROSS_VALIDATION_SYNONYMS: dict[str, str] = {
    # Electronics
    "television": "tv",
    "hdtv": "tv",
    "oled": "tv",
    "qled": "tv",
    "lcd": "tv",
    # Clothing
    "fleece": "jacket",
    "pullover": "jacket",
    "hoodie": "jacket",
    "sweatshirt": "jacket",
    "quarter": "zip",  # "quarter zip" is a style
    # Kitchen
    "refrigerator": "fridge",
    "cappuccino": "espresso",
    "coffeemaker": "coffee",
    # Furniture
    "bookshelf": "shelf",
    "bookcase": "shelf",
    "shelving": "shelf",
    "cabinet": "storage",
    "wardrobe": "closet",
    "armoire": "closet",
    # Tools
    "bandsaw": "saw",
    "tablesaw": "saw",
    "jigsaw": "saw",
    # General
    "automobile": "car",
    "vehicle": "car",
    "bicycle": "bike",
    # Toys & Collectibles
    "plush": "stuffed",
    "plush toy": "stuffed",
    "stuffed animal": "stuffed",
    "stuffed toy": "stuffed",
    "collectible card game": "pokemon",
    "trading card game": "pokemon",
    "tcg": "pokemon",
    # Kitchen appliances
    "slow cooker": "crockpot",
    "single burner": "hotplate",
    "induction burner": "hotplate",
    "pressure cooker": "instapot",
    # Tools
    "cordless drill": "drill",
    "power drill": "drill",
    "circular saw": "saw",
    # Home
    "floating shelf": "shelf",
    "wall shelf": "shelf",
}


def _normalize_synonym(word: str) -> str:
    """Map a word to its canonical synonym form."""
    return _CROSS_VALIDATION_SYNONYMS.get(word, word)


# Short words (< 3 chars) that are significant for product matching.
_SHORT_BUT_SIGNIFICANT = frozenset({"tv", "pc", "hp", "lg", "ge", "dj", "rv", "ac"})


def _significant_words(text: str) -> set[str]:
    """Extract significant lowercase words from text (skip stopwords and short words)."""
    words = re.split(r"[\s\-/,.()\[\]]+", text.lower())
    return {
        w for w in words
        if (len(w) >= 3 and w not in _STOP_WORDS) or w in _SHORT_BUT_SIGNIFICANT
    }


def _word_overlap_ratio(words_a: set[str], words_b: set[str]) -> float:
    """Jaccard-like overlap with synonym normalization.

    Maps words through ``_CROSS_VALIDATION_SYNONYMS`` before comparing,
    so "tv" and "television" both become "tv" and count as a match.
    Returns 0-1 (intersection / min set size).
    """
    if not words_a or not words_b:
        return 0.0
    norm_a = {_normalize_synonym(w) for w in words_a}
    norm_b = {_normalize_synonym(w) for w in words_b}
    intersection = norm_a & norm_b
    return len(intersection) / min(len(norm_a), len(norm_b))


class ValidatedQuery(NamedTuple):
    """Result of cross-validating Vision API enrichment against listing title.

    Attributes:
        query: Validated search query string.
        trusted: True when enrichment matched the listing (overlap >= 0.4 or brand
            found in listing text). When False, the query is the listing title and
            the enrichment's brand/model should NOT be forwarded to downstream
            lookups (eBay, retail MSRP) — they are hallucinations.
    """

    query: str
    trusted: bool


def _build_validated_ebay_query(
    enriched_name: str,
    enriched_brand: str | None,
    listing_title: str,
    listing_description: str | None,
) -> ValidatedQuery:
    """Cross-validate Vision API product name against listing title for eBay query.

    If the Vision API identified a specific branded product that doesn't appear
    in the listing text, prefer the listing title — the seller knows what they're
    selling. If they match, use the more specific enriched name.

    Args:
        enriched_name: Product name from Vision API / SerpAPI.
        enriched_brand: Brand from visual enrichment (may be None).
        listing_title: Seller's listing title.
        listing_description: Seller's listing description (may be None).

    Returns:
        ValidatedQuery with the search string and a ``trusted`` flag indicating
        whether the enrichment was corroborated by the listing text.
    """
    title_lower = (listing_title or "").strip().lower()

    # If listing title is garbage (UI artifacts), use enriched name as-is
    if title_lower in _UI_ARTIFACT_TITLES or len(title_lower) <= 3:
        query = enriched_name
        if enriched_brand and enriched_brand.lower() not in query.lower():
            query = f"{enriched_brand} {query}"
        return ValidatedQuery(query=query, trusted=True)

    enriched_words = _significant_words(enriched_name)
    title_words = _significant_words(listing_title)

    # Also check description for brand/model mentions
    desc_text = (listing_description or "").lower()
    full_listing_text = f"{title_lower} {desc_text}"

    # Check if the enriched brand actually appears in the listing
    brand_in_listing = False
    if enriched_brand:
        brand_lower = enriched_brand.lower()
        brand_in_listing = brand_lower in full_listing_text

    overlap = _word_overlap_ratio(enriched_words, title_words)

    if overlap >= 0.4 or brand_in_listing:
        # Good match — Vision API and listing agree, use enriched (more specific)
        query = enriched_name
        if enriched_brand and enriched_brand.lower() not in query.lower():
            query = f"{enriched_brand} {query}"
        log.debug(
            "eBay query: using enriched name (good overlap)",
            enriched=enriched_name,
            title=listing_title[:50],
            overlap=round(overlap, 2),
        )
        return ValidatedQuery(query=query, trusted=True)

    # Divergence — Vision sees something different than the listing says.
    # Trust the seller's title, not the Vision API's guess.
    query = listing_title
    log.info(
        "eBay query: using listing title (vision mismatch)",
        enriched=enriched_name,
        title=listing_title[:50],
        overlap=round(overlap, 2),
    )
    return ValidatedQuery(query=query, trusted=False)


def _sanitize_untrusted_enrichment(enrichment: VisualEnrichment) -> VisualEnrichment:
    """Strip misleading product identification when Vision API was untrusted.

    When cross-validation shows overlap=0.0 (Vision says "analog watch" but
    listing says "Keurig coffee maker"), the enriched_product_name/brand/model
    are hallucinations. Passing them to the VLM would mislead its evaluation.

    Keeps useful data: OCR text, web entities (may contain real brand names),
    enrichment_tier (so VLM knows enrichment was attempted).
    """
    enrichment.enriched_product_name = None
    enrichment.enriched_brand = None
    enrichment.enriched_model = None
    enrichment.enrichment_confidence = 0.0
    return enrichment


# Maximum MSRP-to-listing-price ratio before we consider the MSRP suspicious.
# If a seller is asking $10 and MSRP comes back as $799, that's almost certainly
# a wrong product match (search returned "entertainment center" for "floating shelf").
# Exception: very cheap listings ($0-5, "free") can legitimately have high MSRP.
_MAX_MSRP_RATIO = 15.0
_MSRP_CHECK_MIN_LISTING_PRICE = 5.0


def _sanity_check_msrp(
    comparables: Any, listing: Listing, query: str
) -> Any:
    """Cross-validate retail MSRP against the listing's asking price.

    If MSRP is wildly disproportionate to listing price (>15x) and the listing
    isn't free/near-free, the MSRP is almost certainly for the wrong product.
    In that case, discard it (return empty result) rather than letting it
    inflate deal scoring.

    Also logs accepted MSRPs for traceability.
    """
    listing_price = listing.price if listing.price is not None else None
    msrp = comparables.median_price or 0.0

    if msrp <= 0 or comparables.sample_count == 0:
        return comparables

    # No listing price → can't validate MSRP ratio, but still keep it
    # for informational display. Log clearly that price is unknown.
    if listing_price is None:
        log.info(
            "Retail lookup provided MSRP (listing price unknown)",
            product=query[:40],
            price=msrp,
        )
        return comparables

    # Free / near-free listings can have any MSRP — seller is giving it away
    if listing_price < _MSRP_CHECK_MIN_LISTING_PRICE:
        log.info(
            "Retail lookup provided MSRP",
            product=query[:40],
            price=msrp,
            listing_price=listing_price,
        )
        return comparables

    ratio = msrp / listing_price
    if ratio > _MAX_MSRP_RATIO:
        log.warning(
            "MSRP rejected as disproportionate",
            product=query[:40],
            msrp=msrp,
            listing_price=listing_price,
            ratio=round(ratio, 1),
        )
        # Return empty comparables — don't let a hallucinated MSRP pollute scoring
        from poob.skills.models import PriceLookupResult

        return PriceLookupResult(
            sample_count=0,
            source="retail",
            search_query=comparables.search_query,
            confidence=0.0,
        )

    log.info(
        "Retail lookup provided MSRP",
        product=query[:40],
        price=msrp,
        listing_price=listing_price,
        ratio=round(ratio, 1),
    )
    return comparables


# Regex to extract seller-stated original price from descriptions.
_SELLER_PRICE_RE = re.compile(
    r"(?:paid|bought\s+(?:it\s+)?for|originally|(?:retail|retails)\s*(?:at|for)?|"
    r"msrp|(?:costs?|valued?\s+at)|was\s+|bought\s+at|new\s+(?:for|at|price))\s*"
    r"\$(\d[\d,]*\.?\d{0,2})",
    re.IGNORECASE,
)


def _extract_seller_stated_price(description: str | None) -> float | None:
    """Extract a price the seller states they originally paid.

    Looks for patterns like "Paid $265", "Originally $300", "Retail $400",
    "MSRP $500", "Was $200" in the listing description.

    Args:
        description: Listing description text.

    Returns:
        The seller-stated price, or None if not found.
    """
    if not description:
        return None
    match = _SELLER_PRICE_RE.search(description)
    if match:
        try:
            price = float(match.group(1).replace(",", ""))
            if 5.0 <= price <= 50_000.0:
                return price
        except ValueError:
            pass
    return None


# --- Condition signal extraction (programmatic, zero API cost) ---

# Positive condition signals — item is in better shape
_POSITIVE_CONDITION_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("sealed/NIB", re.compile(r"\b(?:sealed|nib|bnib|new in box|unopened|shrink.?wrap)\b", re.I)),
    ("like new", re.compile(r"\b(?:like new|mint|pristine|immaculate|flawless)\b", re.I)),
    ("excellent", re.compile(r"\b(?:excellent|great condition|perfect condition)\b", re.I)),
    ("works perfectly", re.compile(r"\b(?:works? (?:perfectly|great|fine)|fully functional|tested)\b", re.I)),
    ("barely used", re.compile(r"\b(?:barely used|hardly used|rarely used|used once|used twice)\b", re.I)),
    ("with box/manual", re.compile(r"\b(?:original box|with box|includes? manual|all accessories)\b", re.I)),
    ("recently purchased", re.compile(r"\b(?:bought (?:last|this) (?:week|month)|just bought|brand new)\b", re.I)),
]

# Negative condition signals — item has issues that reduce value
_NEGATIVE_CONDITION_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("missing parts", re.compile(r"\b(?:missing (?:part|piece|remote|cord|charger|lid|knob|screw)s?)\b", re.I)),
    ("needs repair", re.compile(r"\b(?:needs? (?:repair|fix|work)|broken|damaged|cracked|bent)\b", re.I)),
    ("not working", re.compile(r"\b(?:not working|doesn.?t work|won.?t (?:turn on|start)|dead|defective)\b", re.I)),
    ("scratched/worn", re.compile(r"\b(?:scratch(?:ed|es)|dent(?:ed|s)?|worn|faded|rust(?:ed|y)?|stain(?:ed|s)?)\b", re.I)),
    ("parts only", re.compile(r"\b(?:for parts|parts only|as.?is|salvage)\b", re.I)),
    ("incomplete set", re.compile(r"\b(?:incomplete|missing some|not all|partial set)\b", re.I)),
    ("outdated", re.compile(r"\b(?:outdated|obsolete|discontinued|old model|vintage)\b", re.I)),
]


def extract_condition_signals(listing: Listing) -> dict[str, list[str]]:
    """Extract condition signals from listing title and description.

    Returns a dict with 'positive' and 'negative' lists of matched signal names.
    This is free intelligence — no API calls, instant execution.
    """
    text = f"{listing.title} {listing.description or ''}"
    positive: list[str] = []
    negative: list[str] = []

    for name, pattern in _POSITIVE_CONDITION_PATTERNS:
        if pattern.search(text):
            positive.append(name)

    for name, pattern in _NEGATIVE_CONDITION_PATTERNS:
        if pattern.search(text):
            negative.append(name)

    return {"positive": positive, "negative": negative}


# --- Model number extraction (zero API cost, uses text already available) ---

# Model numbers: 2+ uppercase letters followed by digits (and optional trailing letters/digits).
# Examples: WH-1000XM4, KSM150PSER, DWE7491RS, QN65Q80AAFXZA, QC35II
# Minimum 5 chars to avoid false positives (4K, TV, PS5 are not model numbers).
_MODEL_NUMBER_RE = re.compile(
    r"\b([A-Z]{2,}[\-]?\d{2,}[A-Z0-9]*)\b"
    r"|"
    r"\b([A-Z][A-Z0-9]{2,}\d[A-Z0-9]*)\b"
)

# Exclude common non-model patterns
_MODEL_NUMBER_EXCLUDE = re.compile(
    r"^(?:4K|8K|TV|PS[1-5]|HD|USB|LED|LCD|DVD|VHS|RAM|SSD|HDD|CPU|GPU|"
    r"RGB|HDMI|WiFi|OLED|QLED|RPM|MPH|OBO|NIB|NWT|EUC|OEM|DIY|USA|USD)$",
    re.IGNORECASE,
)


def extract_model_numbers(text: str) -> list[str]:
    """Extract product model numbers from text (title, description, OCR).

    Finds alphanumeric patterns that look like model numbers (e.g., WH-1000XM4,
    DWE7491RS, KSM150PSER). Filters out common abbreviations and short codes.

    Args:
        text: Input text to scan for model numbers.

    Returns:
        List of unique model number strings found.
    """
    if not text:
        return []

    models: list[str] = []
    seen: set[str] = set()

    for match in _MODEL_NUMBER_RE.finditer(text.upper()):
        candidate = match.group(1) or match.group(2)
        if not candidate or len(candidate) < 5:
            continue
        if _MODEL_NUMBER_EXCLUDE.match(candidate):
            continue
        if candidate not in seen:
            seen.add(candidate)
            models.append(candidate)

    return models


# --- Title quality heuristics (programmatic spam/scam detection) ---

_EMOJI_RE = re.compile(
    r"[\U0001F300-\U0001F9FF\U00002702-\U000027B0\U0000FE00-\U0000FE0F"
    r"\U0001FA00-\U0001FA6F\U0001FA70-\U0001FAFF\U00002600-\U000026FF"
    r"\U0000200D\U00002B50\U00002B55\U000023F0-\U000023FA\U0001F680-\U0001F6FF]+"
)

_SPAM_WORDS = frozenset({
    "look", "wow", "hurry", "amazing", "incredible", "insane", "steal",
    "must see", "won't last", "act fast", "best deal", "lowest price",
    "urgent", "today only", "limited time", "don't miss",
})


def compute_title_quality_score(title: str) -> float:
    """Score a listing title's quality from 0.0 (spam) to 1.0 (clean).

    Penalizes:
    - ALL CAPS titles (shouting)
    - Excessive emojis
    - Excessive punctuation (!!!, ???)
    - Spam/bait keywords
    - Very short titles (low information)

    Returns:
        Float between 0.0 and 1.0.
    """
    if not title:
        return 0.0

    score = 1.0

    # ALL CAPS penalty (check only alpha chars)
    alpha_chars = [c for c in title if c.isalpha()]
    if len(alpha_chars) >= 5:
        caps_ratio = sum(1 for c in alpha_chars if c.isupper()) / len(alpha_chars)
        if caps_ratio > 0.7:
            score -= 0.3

    # Emoji density penalty
    emoji_count = len(_EMOJI_RE.findall(title))
    if emoji_count >= 3:
        score -= 0.3
    elif emoji_count >= 1:
        score -= 0.1

    # Excessive punctuation penalty
    excl_count = title.count("!") + title.count("?")
    if excl_count >= 4:
        score -= 0.25
    elif excl_count >= 2:
        score -= 0.1

    # Spam word penalty
    title_lower = title.lower()
    spam_hits = sum(1 for word in _SPAM_WORDS if word in title_lower)
    if spam_hits >= 3:
        score -= 0.45
    elif spam_hits >= 2:
        score -= 0.3
    elif spam_hits >= 1:
        score -= 0.15

    # Short title penalty (low information content)
    word_count = len(title.split())
    if word_count <= 1:
        score -= 0.3
    elif word_count <= 2:
        score -= 0.15

    return max(0.0, min(1.0, score))


# --- Listing freshness scoring ---


def compute_freshness_bonus(listing: Listing) -> float:
    """Compute a freshness bonus based on how recently the listing was posted.

    Newer listings are more likely to still be available and represent genuine
    deals (not stale posts that nobody wanted).

    Returns:
        Float between 0.0 (very old) and 1.0 (just posted).
        0.5 if posted_at is unknown.
    """
    from datetime import datetime, timezone

    if not listing.posted_at:
        return 0.5

    now = datetime.now(timezone.utc)
    posted = listing.posted_at
    if posted.tzinfo is None:
        posted = posted.replace(tzinfo=timezone.utc)

    age_hours = (now - posted).total_seconds() / 3600.0

    if age_hours <= 0:
        return 1.0
    elif age_hours <= 1:
        return 1.0 - (age_hours * 0.1)  # 0.9-1.0
    elif age_hours <= 6:
        return 0.9 - ((age_hours - 1) * 0.1)  # 0.4-0.9
    elif age_hours <= 24:
        return 0.4 - ((age_hours - 6) * 0.015)  # 0.13-0.4
    else:
        # After 24h, decay slowly to near-zero
        return max(0.05, 0.13 - ((age_hours - 24) * 0.002))


# --- Multi-item / lot detection with price normalization ---

_QUANTITY_PATTERNS: list[tuple[re.Pattern[str], int | None]] = [
    # "3 Lego sets", "5 games", "10 books" — explicit number before noun
    (re.compile(r"\b(\d{1,3})\s+\w+(?:\s+\w+)?\s*(?:set|game|book|item|piece|card|"
                r"shirt|shoe|pair|toy|tool|plant|pot|cup|plate|glass|bowl|jar|"
                r"bottle|bag|box|can|pack|dvd|cd|vinyl|record|comic|figure|"
                r"car|wheel|tire|speaker|controller|cable|cord|bulb|"
                r"shirt|pant|dress|hat|scarf)s?\b", re.I), None),
    # "Set of 6", "Lot of 20", "Box of 10", "Pack of 12"
    (re.compile(r"\b(?:set|lot|box|pack|case|bundle|group|collection)\s+of\s+(\d{1,3})\b", re.I), None),
    # "pair of" → 2
    (re.compile(r"\bpair\s+of\b", re.I), 2),
    # "5 games" in "bundle - 5 games"
    (re.compile(r"\bbundle\b.*?(\d{1,3})\s+\w+", re.I), None),
    # Explicit "x5", "×3" multipliers
    (re.compile(r"\b[x×](\d{1,3})\b", re.I), None),
]


def detect_multi_item(
    title: str, description: str, listing_price: float
) -> dict[str, Any] | None:
    """Detect multi-item listings and compute per-unit price.

    Scans title and description for quantity signals (lot, set of N, pair,
    bundle, explicit counts). Returns quantity + per-unit price for deal
    evaluation context.

    Args:
        title: Listing title.
        description: Listing description.
        listing_price: Listing asking price.

    Returns:
        Dict with 'quantity' and 'per_unit_price', or None if single item.
    """
    text = f"{title} {description or ''}"

    for pattern, fixed_qty in _QUANTITY_PATTERNS:
        match = pattern.search(text)
        if match:
            if fixed_qty is not None:
                qty = fixed_qty
            else:
                qty = int(match.group(1))

            if qty < 2 or qty > 500:
                continue

            per_unit = listing_price / qty if qty > 0 else 0.0
            return {
                "quantity": qty,
                "per_unit_price": round(per_unit, 2),
            }

    return None


def _gather_additional_intelligence(
    listing: Listing,
    enrichment: VisualEnrichment | None,
) -> dict[str, Any]:
    """Gather all programmatic intelligence signals for a listing.

    Collects model numbers, title quality, freshness, and multi-item detection
    into a single dict for the VLM prompt. All zero-cost, instant execution.

    Args:
        listing: The marketplace listing.
        enrichment: Visual enrichment data (may have OCR text).

    Returns:
        Dict of intelligence signals for the VLM prompt.
    """
    intel: dict[str, Any] = {}

    # Model numbers from title, description, and OCR
    text_sources = [listing.title, listing.description or ""]
    if enrichment and enrichment.ocr_text:
        text_sources.append(enrichment.ocr_text)
    all_text = " ".join(text_sources)
    models = extract_model_numbers(all_text)
    if models:
        intel["model_numbers"] = models

    # Title quality score
    tq = compute_title_quality_score(listing.title)
    if tq < 0.8:  # Only include when noteworthy
        intel["title_quality_score"] = round(tq, 2)

    # Freshness bonus
    fb = compute_freshness_bonus(listing)
    intel["freshness_bonus"] = round(fb, 2)

    # Multi-item detection
    mi = detect_multi_item(
        listing.title, listing.description or "", listing.price or 0.0
    )
    if mi:
        intel["multi_item"] = mi

    return intel


class SmartDealRadar:
    """VLM-driven deal evaluation pipeline.

    Stage 1: Text triage (batch filter)
    Stage 2: Visual enrichment + comparable sales
    Stage 3: VLM deep evaluation

    Args:
        text_triage: Batched text triage service.
        visual_enrichment: Visual enrichment service (Vision API / SerpAPI).
        vlm_evaluator: VLM deal evaluator with provider cascade.
        ebay_lookup: eBay sold price lookup tool.
        retail_lookup: Retail/MSRP price lookup tool.
        min_deal_quality: Minimum deal quality to create Deal objects.
    """

    def __init__(
        self,
        *,
        text_triage: TextTriageService,
        visual_enrichment: VisualEnrichmentService,
        vlm_evaluator: VLMDealEvaluator,
        ebay_lookup: EbayLookupTool | None = None,
        retail_lookup: RetailLookupTool | None = None,
        search_cascade: SearchProviderCascade | None = None,
        min_deal_quality: str = "good",
        incredible_abs_dollar_floor: float = 50.0,
        max_value_multiple: float = 4.0,
        free_item_min_value: float = 40.0,
        free_item_incredible_min_value: float = 80.0,
    ) -> None:
        self._text_triage = text_triage
        self._visual_enrichment = visual_enrichment
        self._vlm_evaluator = vlm_evaluator
        self._ebay_lookup = ebay_lookup
        self._retail_lookup = retail_lookup
        self._search_cascade = search_cascade
        self._min_score = _QUALITY_TO_SCORE.get(min_deal_quality, DealScore.GOOD)
        # PUBLIC-only selectivity tunables (watchlist matches are exempt). See
        # docs/decisions/public-incredible-selectivity-floors.md.
        self._incredible_abs_floor = incredible_abs_dollar_floor
        self._max_value_multiple = max_value_multiple
        self._free_min_value = free_item_min_value
        self._free_incredible_min_value = free_item_incredible_min_value

    async def evaluate_batch(
        self,
        listings: list[Listing],
        watchlist_items: list[WatchItem] | None = None,
    ) -> list[tuple[Deal | None, VLMEvaluation]]:
        """Evaluate a batch of listings through the full pipeline.

        Args:
            listings: New listings to evaluate.
            watchlist_items: Active watchlist items for triage context.

        Returns:
            List of (Deal or None, VLMEvaluation) tuples.
        """
        if not listings:
            return []

        t0 = time.monotonic()

        # Pre-triage: separate listings that should bypass text triage.
        # Only low-quality text (e.g. "Just listed" with no description)
        # bypasses — VLM with image access is the only useful evaluator.
        triage_listings: list[Listing] = []
        auto_investigate: list[Listing] = []

        for listing in listings:
            reason = _should_bypass_triage(listing)
            if reason:
                log.debug(
                    "Triage bypass",
                    title=listing.title[:40],
                    reason=reason,
                )
                auto_investigate.append(listing)
            else:
                triage_listings.append(listing)

        if auto_investigate:
            log.info(
                "Triage bypass",
                auto_investigate=len(auto_investigate),
                to_triage=len(triage_listings),
            )

        # Stage 1: Text triage (batched) — only for listings with usable text
        log.info("Stage 1: Text triage", listings=len(triage_listings))
        triage_results = await self._text_triage.triage_batch(
            triage_listings, watchlist_items
        ) if triage_listings else []

        # Merge: triage survivors + auto-investigate bypass listings
        investigative: list[tuple[Listing, TriageResult]] = [
            (listing, triage)
            for listing, triage in zip(triage_listings, triage_results)
            if triage.investigate
        ]

        # Auto-investigate listings get a synthetic triage result
        for listing in auto_investigate:
            bypass_reason = _should_bypass_triage(listing) or "auto_investigate"
            investigative.append((
                listing,
                TriageResult(
                    listing_id=listing.id or "",
                    investigate=True,
                    reasoning=f"triage_bypass:{bypass_reason}",
                ),
            ))

        filtered = len(listings) - len(investigative)

        if not investigative:
            log.info("Stage 1: All listings filtered out")
            return [
                (None, VLMEvaluation(reasoning="Filtered by triage"))
                for _ in listings
            ]

        log.info(
            "Stage 1 complete",
            investigate=len(investigative),
            filtered=filtered,
        )

        # Pre-enrichment preference filter: cheap programmatic check that
        # catches preference contradictions (e.g. "only cups" vs "vase",
        # bulk constraints, negation patterns) BEFORE burning Vision API
        # calls, eBay lookups, and VLM evaluations.
        if watchlist_items:
            pre_filter_count = len(investigative)
            investigative = _pre_enrichment_preference_filter(
                investigative, watchlist_items
            )
            pref_filtered = pre_filter_count - len(investigative)
            if pref_filtered:
                log.info(
                    "Pre-enrichment preference filter",
                    removed=pref_filtered,
                    remaining=len(investigative),
                )

        if not investigative:
            log.info("All listings filtered by preferences")
            return [
                (None, VLMEvaluation(reasoning="Filtered by preferences"))
                for _ in listings
            ]

        # Stage 2: Visual enrichment + comparable sales (concurrent)
        log.info("Stage 2: Visual enrichment", count=len(investigative))
        enriched = await self._enrich_batch(investigative)

        # Stage 3: VLM deep evaluation (semaphore-throttled)
        has_desc = sum(1 for l, *_ in enriched if (l.description or "").strip())
        has_price = sum(1 for l, *_ in enriched if l.price is not None)
        log.info(
            "Stage 3: VLM evaluation",
            count=len(enriched),
            with_description=has_desc,
            with_price=has_price,
        )
        results = await self._evaluate_batch(enriched, watchlist_items)

        elapsed = time.monotonic() - t0
        deals = sum(1 for deal, _ in results if deal is not None)
        log.info(
            "Pipeline complete",
            total_listings=len(listings),
            investigated=len(investigative),
            deals_found=deals,
            elapsed=f"{elapsed:.1f}s",
        )

        return results

    async def evaluate(
        self, listing: Listing
    ) -> tuple[Deal | None, VLMEvaluation]:
        """Evaluate a single listing (convenience wrapper).

        Args:
            listing: The marketplace listing to evaluate.

        Returns:
            Tuple of (Deal if the listing is a deal else None, VLMEvaluation).
        """
        results = await self.evaluate_batch([listing])
        return results[0] if results else (None, VLMEvaluation())

    async def evaluate_streaming(
        self,
        listings: list[Listing],
        watchlist_items: list[WatchItem] | None = None,
        on_deal_found: Callable[
            [Deal, VLMEvaluation, Listing], Coroutine[Any, Any, None]
        ] | None = None,
    ) -> list[tuple[Deal | None, VLMEvaluation]]:
        """Streaming pipeline: listings flow through stages via asyncio.Queue.

        Unlike evaluate_batch which processes all listings through each stage
        before moving to the next, this method lets each listing advance
        independently. The first deal can be found and notified while other
        listings are still being triaged.

        Args:
            listings: New listings to evaluate.
            watchlist_items: Active watchlist items for triage context.
            on_deal_found: Optional async callback invoked immediately when
                a deal is found (e.g. to send a Discord notification).

        Returns:
            List of (Deal or None, VLMEvaluation) tuples (same as evaluate_batch).
        """
        if not listings:
            return []

        t0 = time.monotonic()

        # Queues between stages (bounded to prevent memory blowup)
        enrich_q: asyncio.Queue = asyncio.Queue(maxsize=30)
        eval_q: asyncio.Queue = asyncio.Queue(maxsize=10)

        results: dict[str, tuple[Deal | None, VLMEvaluation]] = {}
        listing_ids = [l.id or str(i) for i, l in enumerate(listings)]

        # --- Stage 1 producer: triage + preference filter → enrich_q ---
        async def triage_producer() -> None:
            triage_listings: list[Listing] = []
            bypass_listings: list[Listing] = []

            for listing in listings:
                reason = _should_bypass_triage(listing)
                if reason:
                    bypass_listings.append(listing)
                else:
                    triage_listings.append(listing)

            # Batch triage (this is already efficient as a batch)
            triage_results = (
                await self._text_triage.triage_batch(triage_listings, watchlist_items)
                if triage_listings
                else []
            )

            # Emit survivors
            for listing, triage in zip(triage_listings, triage_results):
                if triage.investigate:
                    # Check pre-enrichment preferences
                    if watchlist_items:
                        items_check = [(listing, triage)]
                        items_check = _pre_enrichment_preference_filter(
                            items_check, watchlist_items
                        )
                        if not items_check:
                            lid = listing.id or ""
                            results[lid] = (
                                None,
                                VLMEvaluation(reasoning="Filtered by preferences"),
                            )
                            continue
                    await enrich_q.put((listing, triage))
                else:
                    lid = listing.id or ""
                    results[lid] = (
                        None,
                        VLMEvaluation(reasoning="Filtered by triage"),
                    )

            # Emit bypass listings
            for listing in bypass_listings:
                bypass_reason = _should_bypass_triage(listing) or "auto_investigate"
                triage = TriageResult(
                    listing_id=listing.id or "",
                    investigate=True,
                    reasoning=f"triage_bypass:{bypass_reason}",
                )
                await enrich_q.put((listing, triage))

            await enrich_q.put(_DONE)

        # --- Stage 2 workers: enrich_q → eval_q ---
        enrich_sem = asyncio.Semaphore(3)

        async def enrich_worker() -> None:
            while True:
                item = await enrich_q.get()
                if item is _DONE:
                    enrich_q.task_done()
                    break

                listing, triage = item
                async with enrich_sem:
                    enrichment = VisualEnrichment()
                    comparables = None
                    validated = ValidatedQuery(query=listing.title, trusted=False)
                    try:
                        if listing.image_urls:
                            enrichment = await self._visual_enrichment.enrich(
                                listing.image_urls[0]
                            )
                        if enrichment.enriched_product_name and self._ebay_lookup:
                            validated = _build_validated_ebay_query(
                                enriched_name=enrichment.enriched_product_name,
                                enriched_brand=enrichment.enriched_brand,
                                listing_title=listing.title,
                                listing_description=listing.description,
                            )
                            # Feed cross-validation result back to Vision API
                            # adaptive disabler so it can auto-disable after
                            # too many consecutive hallucinations.
                            self._visual_enrichment.report_vision_result(
                                matched=validated.trusted,
                            )
                            comparables = await self._ebay_lookup.run(validated.query)
                        if (
                            self._retail_lookup
                            and (comparables is None or comparables.sample_count == 0)
                        ):
                            comparables = await self._retail_lookup.run(
                                validated.query,
                                brand=enrichment.enriched_brand if validated.trusted else None,
                                model=enrichment.enriched_model if validated.trusted else None,
                            )
                            if comparables.sample_count > 0:
                                comparables = _sanity_check_msrp(
                                    comparables, listing, validated.query,
                                )
                    except Exception as exc:
                        log.warning(
                            "Streaming enrichment failed",
                            listing_id=listing.id,
                            error=str(exc)[:100],
                        )

                    # Strip misleading Vision API data before VLM sees it.
                    # When Vision said "analog watch" for a Keurig, don't
                    # tell the VLM the item is an analog watch.
                    if not validated.trusted:
                        _sanitize_untrusted_enrichment(enrichment)

                    await eval_q.put((listing, triage, enrichment, comparables))
                enrich_q.task_done()

        # --- Stage 3 workers: eval_q → results ---
        eval_sem = asyncio.Semaphore(2)

        async def eval_worker() -> None:
            while True:
                item = await eval_q.get()
                if item is _DONE:
                    eval_q.task_done()
                    break

                listing, triage, enrichment, comparables = item
                async with eval_sem:
                    try:
                        watchlist_context = _build_watchlist_context(
                            listing, watchlist_items
                        )
                        seller_price = _extract_seller_stated_price(
                            listing.description
                        )
                        cond_signals = extract_condition_signals(listing)
                        intel = _gather_additional_intelligence(
                            listing, enrichment
                        )

                        # Fire web search in parallel with VLM (adds ~0 latency)
                        web_task = asyncio.create_task(
                            self._web_context_search(
                                listing, enrichment, intel.get("model_numbers"),
                            )
                        )

                        # Build provenance trail
                        provenance = DealProvenance(
                            triage_bypass_reason=(
                                triage.reasoning if not triage.investigate else ""
                            ),
                            scam_signals=triage.scam_signals,
                            urgency_signals=triage.urgency_signals,
                            enrichment_tier=getattr(enrichment, "enrichment_tier", 3),
                            enrichment_product=getattr(
                                enrichment, "enriched_product_name", ""
                            ) or "",
                            price_source=getattr(comparables, "source", "") if comparables else "",
                            price_confidence=getattr(comparables, "confidence", 0.0) if comparables else 0.0,
                            price_sample_count=getattr(comparables, "sample_count", 0) if comparables else 0,
                        )

                        vlm_result = await self._vlm_evaluator.evaluate(
                            listing=listing,
                            enrichment=enrichment,
                            comparable_prices=comparables,
                            triage=triage,
                            watchlist_context=watchlist_context,
                            seller_stated_price=seller_price,
                            condition_signals=cond_signals,
                            additional_intelligence=intel,
                        )

                        # Populate VLM provenance
                        vlm_cascade = getattr(self._vlm_evaluator, "_vlm", None)
                        if vlm_cascade:
                            provenance.vlm_providers = list(
                                getattr(vlm_cascade, "last_responding_providers", [])
                            )
                            provenance.vlm_agreement = getattr(
                                vlm_cascade, "last_agreement_confidence", None
                            )
                        provenance.vlm_raw_quality = vlm_result.deal_quality
                        provenance.vlm_condition = vlm_result.condition
                        provenance.vlm_condition_notes = vlm_result.condition_notes[:80]
                        provenance.vlm_confidence = vlm_result.confidence
                        provenance.vlm_item_identified = vlm_result.item_identified[:80]

                        # Collect web context — capture in provenance, NOT in reasoning
                        web_ctx = await web_task
                        if web_ctx:
                            provenance.web_search_used = True

                        # Cap VLM estimate at seller-stated price (+ 10% tolerance)
                        if seller_price and vlm_result.estimated_value_mid > 0:
                            cap = seller_price * 1.1
                            if vlm_result.estimated_value_mid > cap:
                                log.info(
                                    "Value capped at seller-stated price",
                                    original_estimate=vlm_result.estimated_value_mid,
                                    seller_price=seller_price,
                                    capped_to=round(cap, 2),
                                    title=listing.title[:50],
                                )
                                vlm_result.estimated_value_mid = cap
                                vlm_result.estimated_value_high = min(
                                    vlm_result.estimated_value_high, cap * 1.2
                                )

                        deal = self._vlm_to_deal(
                            listing, vlm_result, watchlist_context, enrichment,
                            provenance=provenance,
                            comparables=comparables,
                        )

                        lid = listing.id or ""
                        results[lid] = (deal, vlm_result)

                        # Immediate callback for deals
                        if deal and on_deal_found:
                            try:
                                await on_deal_found(deal, vlm_result, listing)
                            except Exception as exc:
                                log.warning(
                                    "on_deal_found callback failed",
                                    error=str(exc)[:100],
                                )
                    except Exception as exc:
                        lid = listing.id or ""
                        results[lid] = (
                            None,
                            VLMEvaluation(reasoning=f"VLM error: {str(exc)[:80]}"),
                        )
                eval_q.task_done()

        # --- Run pipeline ---
        # Start enrichment workers (3 concurrent)
        enrich_workers = [asyncio.create_task(enrich_worker()) for _ in range(3)]

        # Start eval workers (2 concurrent)
        eval_workers = [asyncio.create_task(eval_worker()) for _ in range(2)]

        # Run triage producer
        await triage_producer()

        # Wait for enrichment to finish, then signal eval workers
        await asyncio.gather(*enrich_workers)
        for _ in eval_workers:
            await eval_q.put(_DONE)

        # Wait for eval to finish
        await asyncio.gather(*eval_workers)

        elapsed = time.monotonic() - t0
        deals = sum(1 for d, _ in results.values() if d is not None)
        log.info(
            "Streaming pipeline complete",
            total_listings=len(listings),
            evaluated=len(results),
            deals_found=deals,
            elapsed=f"{elapsed:.1f}s",
        )

        # Return in original listing order
        ordered: list[tuple[Deal | None, VLMEvaluation]] = []
        for lid in listing_ids:
            if lid in results:
                ordered.append(results[lid])
            else:
                ordered.append((None, VLMEvaluation(reasoning="Not processed")))

        return ordered

    async def _web_context_search(
        self,
        listing: Listing,
        enrichment: VisualEnrichment,
        model_numbers: list[str] | None = None,
    ) -> str:
        """Fire a web search for product context (runs parallel to VLM).

        Searches for the product name + model number to find specs, reviews,
        common defects, and market pricing context. Returns summarized text
        or empty string if no search is available.

        This adds ~0 extra latency because it runs concurrently with the
        VLM evaluation (which takes 2-25 seconds).
        """
        if not self._search_cascade:
            return ""

        # Build the best possible search query
        parts: list[str] = []
        if enrichment.enriched_product_name:
            parts.append(enrichment.enriched_product_name)
        elif listing.title:
            parts.append(listing.title)

        if model_numbers:
            parts.append(model_numbers[0])  # First model number is most likely correct

        if not parts:
            return ""

        query = " ".join(parts) + " review price specs"

        try:
            result = await self._search_cascade.search(query)
            if result:
                # Truncate to avoid overwhelming the VLM prompt
                return result[:500]
        except Exception as exc:
            log.debug(
                "web_context_search.failed",
                error=str(exc)[:100],
            )

        return ""

    async def _enrich_batch(
        self,
        items: list[tuple[Listing, TriageResult]],
    ) -> list[tuple[Listing, TriageResult, VisualEnrichment, Any]]:
        """Stage 2: Visual enrichment + comparable sales for each listing."""
        sem = asyncio.Semaphore(3)

        async def _enrich_one(
            listing: Listing, triage: TriageResult
        ) -> tuple[Listing, TriageResult, VisualEnrichment, Any]:
            async with sem:
                # Visual enrichment
                enrichment = VisualEnrichment()
                if listing.image_urls:
                    try:
                        enrichment = await self._visual_enrichment.enrich(
                            listing.image_urls[0]
                        )
                    except Exception as exc:
                        log.warning(
                            "Visual enrichment failed",
                            listing_id=listing.id,
                            error=str(exc)[:100],
                        )

                # Comparable sales (only if we got a real product name)
                comparables = None
                validated = ValidatedQuery(query=listing.title, trusted=False)
                if (
                    enrichment.enriched_product_name
                    and self._ebay_lookup is not None
                ):
                    validated = _build_validated_ebay_query(
                        enriched_name=enrichment.enriched_product_name,
                        enriched_brand=enrichment.enriched_brand,
                        listing_title=listing.title,
                        listing_description=listing.description,
                    )
                    # Feed cross-validation back for adaptive disabling
                    self._visual_enrichment.report_vision_result(
                        matched=validated.trusted,
                    )
                    try:
                        comparables = await self._ebay_lookup.run(validated.query)
                    except Exception as exc:
                        log.warning(
                            "eBay lookup failed",
                            query=validated.query[:50],
                            error=str(exc)[:100],
                        )

                # Retail/MSRP fallback: if eBay found no sold data, try retail.
                # Only forward enrichment brand/model when cross-validation
                # trusted the enrichment — otherwise they are hallucinations
                # (e.g., "dagger" for a Chefman Electric Knife).
                if (
                    validated.query
                    and self._retail_lookup is not None
                    and (comparables is None or comparables.sample_count == 0)
                ):
                    try:
                        comparables = await self._retail_lookup.run(
                            validated.query,
                            brand=enrichment.enriched_brand if validated.trusted else None,
                            model=enrichment.enriched_model if validated.trusted else None,
                        )
                        if comparables.sample_count > 0:
                            comparables = _sanity_check_msrp(
                                comparables, listing, validated.query,
                            )
                    except Exception as exc:
                        log.warning(
                            "Retail lookup failed",
                            error=str(exc)[:100],
                        )

                # Strip misleading Vision API data before VLM sees it
                if not validated.trusted:
                    _sanitize_untrusted_enrichment(enrichment)

                return listing, triage, enrichment, comparables

        results = await asyncio.gather(
            *(_enrich_one(l, t) for l, t in items),
            return_exceptions=True,
        )

        # Filter out exceptions
        valid: list[tuple[Listing, TriageResult, VisualEnrichment, Any]] = []
        for i, r in enumerate(results):
            if isinstance(r, BaseException):
                log.warning(
                    "Enrichment failed for listing",
                    listing_id=items[i][0].id,
                    error=str(r)[:100],
                )
                # Still process it with empty enrichment
                listing, triage = items[i]
                valid.append((listing, triage, VisualEnrichment(), None))
            else:
                valid.append(r)

        return valid

    async def _evaluate_batch(
        self,
        enriched_items: list[tuple[Listing, TriageResult, VisualEnrichment, Any]],
        watchlist_items: list[WatchItem] | None,
    ) -> list[tuple[Deal | None, VLMEvaluation]]:
        """Stage 3: VLM evaluation for each enriched listing."""
        sem = asyncio.Semaphore(2)  # Tighter semaphore for VLM calls

        async def _eval_one(
            listing: Listing,
            triage: TriageResult,
            enrichment: VisualEnrichment,
            comparables: Any,
        ) -> tuple[Deal | None, VLMEvaluation]:
            async with sem:
                # Build watchlist context if applicable
                watchlist_context = _build_watchlist_context(
                    listing, watchlist_items
                )

                try:
                    seller_price = _extract_seller_stated_price(
                        listing.description
                    )
                    cond_signals = extract_condition_signals(listing)
                    intel = _gather_additional_intelligence(
                        listing, enrichment
                    )

                    # Fire web search in parallel with VLM (adds ~0 latency)
                    web_task = asyncio.create_task(
                        self._web_context_search(
                            listing, enrichment, intel.get("model_numbers"),
                        )
                    )

                    # Build provenance trail
                    provenance = _build_provenance(
                        triage, enrichment, comparables
                    )

                    vlm_result = await self._vlm_evaluator.evaluate(
                        listing=listing,
                        enrichment=enrichment,
                        comparable_prices=comparables,
                        triage=triage,
                        watchlist_context=watchlist_context,
                        seller_stated_price=seller_price,
                        condition_signals=cond_signals,
                        additional_intelligence=intel,
                    )

                    # Populate VLM provenance (guard against mocks in tests)
                    vlm_cascade = getattr(self._vlm_evaluator, "_vlm", None)
                    providers = getattr(vlm_cascade, "last_responding_providers", None)
                    if isinstance(providers, list):
                        provenance.vlm_providers = providers
                    agreement = getattr(vlm_cascade, "last_agreement_confidence", None)
                    if isinstance(agreement, (float, int, type(None))):
                        provenance.vlm_agreement = agreement
                    provenance.vlm_raw_quality = vlm_result.deal_quality
                    provenance.vlm_condition = vlm_result.condition
                    provenance.vlm_condition_notes = vlm_result.condition_notes[:80]
                    provenance.vlm_confidence = vlm_result.confidence
                    provenance.vlm_item_identified = vlm_result.item_identified[:80]

                    # Collect web context — capture in provenance, NOT in reasoning
                    web_ctx = await web_task
                    if web_ctx:
                        provenance.web_search_used = True

                    # Cap VLM estimate at seller-stated price (+ 10% tolerance)
                    if seller_price and vlm_result.estimated_value_mid > 0:
                        cap = seller_price * 1.1
                        if vlm_result.estimated_value_mid > cap:
                            log.info(
                                "Value capped at seller-stated price",
                                original_estimate=vlm_result.estimated_value_mid,
                                seller_price=seller_price,
                                capped_to=round(cap, 2),
                                title=listing.title[:50],
                            )
                            vlm_result.estimated_value_mid = cap
                            vlm_result.estimated_value_high = min(
                                vlm_result.estimated_value_high, cap * 1.2
                            )
                except Exception as exc:
                    log.warning(
                        "VLM evaluation failed",
                        listing_id=listing.id,
                        error=str(exc)[:100],
                    )
                    return None, VLMEvaluation(
                        reasoning=f"VLM error: {str(exc)[:80]}"
                    )

                # Convert VLM output to Deal object
                deal = self._vlm_to_deal(
                    listing, vlm_result, watchlist_context, enrichment,
                    provenance=provenance,
                    comparables=comparables,
                )
                return deal, vlm_result

        results = await asyncio.gather(
            *(
                _eval_one(l, t, e, c)
                for l, t, e, c in enriched_items
            ),
            return_exceptions=True,
        )

        # Filter out exceptions
        valid: list[tuple[Deal | None, VLMEvaluation]] = []
        for r in results:
            if isinstance(r, BaseException):
                valid.append((None, VLMEvaluation(reasoning=f"Error: {r}")))
            else:
                valid.append(r)

        return valid

    def _vlm_to_deal(
        self,
        listing: Listing,
        vlm: VLMEvaluation,
        watchlist_context: dict[str, Any] | None,
        enrichment: VisualEnrichment | None = None,
        provenance: DealProvenance | None = None,
        comparables: Any = None,
    ) -> Deal | None:
        """Convert a VLM evaluation to a Deal object (or None if below threshold).

        Applies hard programmatic filters that the VLM cannot override:
        1. Preference contradiction filter (user notes)
        2. Misleading listing detection (trades, popups, bait pricing)
        3. Unbranded generic item value cap
        4. Dollar savings minimums (VLMs hallucinate deal quality on cheap items)
        5. Sanity check: estimated value must exceed listing price
        """
        # Hard preference filter: reject listings that contradict user notes
        if watchlist_context and watchlist_context.get("notes"):
            contradiction = _listing_contradicts_notes(
                listing, watchlist_context["notes"]
            )
            if contradiction:
                log.info(
                    "Listing rejected by preference filter",
                    title=listing.title[:50],
                    excluded_term=contradiction,
                    notes=watchlist_context["notes"][:80],
                )
                return None

        # Misleading listing filter: trades, popups, bait pricing
        misleading = _detect_misleading_listing(listing)
        if misleading:
            log.info(
                "Listing rejected as misleading",
                title=listing.title[:50],
                reason=misleading,
            )
            return None

        # Unbranded generic item value cap — if no brand/model identified from
        # ANY source, cap the estimated value to prevent misidentification inflation.
        # Exception: if the VLM confidently identified a specific brand/model from the
        # photos (e.g., it can see a logo), trust the VLM over the text-based check.
        # High-value categories (TVs, electronics, appliances) get a higher cap since
        # even unbranded items in these categories can legitimately exceed $100.
        # "Max" effort watchlist items bypass the cap entirely — the user explicitly
        # wants thorough evaluation and trusts the VLM's assessment.
        effort = (watchlist_context or {}).get("effort", "normal")
        cap = _get_unbranded_value_cap(listing.title)
        vlm_identified_brand = _vlm_identified_known_brand(vlm.item_identified)

        # Retail MSRP floor: if the retail lookup found a real price for this
        # item, use it as evidence that the arbitrary cap is too low. A retail
        # lookup searched the actual internet — its MSRP is evidence-based,
        # while the $100 cap is a guess for generic unknowns.
        retail_msrp = 0.0
        if comparables and hasattr(comparables, "median_price"):
            retail_msrp = comparables.median_price or 0.0
        if retail_msrp > cap:
            # Use 80% of retail MSRP (used-item depreciation) as the floor,
            # but never lower than the original cap.
            cap = max(cap, retail_msrp * 0.8)
            log.info(
                "Unbranded cap raised by retail MSRP evidence",
                retail_msrp=retail_msrp,
                effective_cap=round(cap, 2),
                title=listing.title[:50],
            )

        if (
            effort != "max"
            and enrichment
            and not enrichment.enriched_brand
            and not enrichment.enriched_model
            and vlm.estimated_value_mid > cap
            and _listing_has_no_brand(listing)
            and not (vlm_identified_brand and vlm.confidence >= 0.7)
        ):
            log.info(
                "Value capped for unbranded generic item",
                original_estimate=vlm.estimated_value_mid,
                capped_to=cap,
                title=listing.title[:50],
            )
            vlm.estimated_value_mid = cap
            vlm.estimated_value_high = min(
                vlm.estimated_value_high, cap * 1.5
            )
        elif vlm_identified_brand and vlm.confidence >= 0.7:
            log.debug(
                "Unbranded cap bypassed — VLM identified brand from photos",
                vlm_brand=vlm_identified_brand,
                confidence=vlm.confidence,
                title=listing.title[:50],
            )

        score = _QUALITY_TO_SCORE.get(vlm.deal_quality, DealScore.FAIR)

        # PUBLIC-only: a "not worth attention" verdict (cheap commodity,
        # consumable, non-item) caps the score below the public INCREDIBLE bar.
        # Fail-open (defaults True), so this only bites on an explicit False.
        # Watchlist matches are exempt — gated solely by their own threshold.
        if watchlist_context is None and not getattr(vlm, "worth_attention", True):
            if _SCORE_RANK.get(score, 0) > _SCORE_RANK[DealScore.FAIR]:
                if provenance:
                    provenance.score_adjustments.append(
                        f"{score.value}->fair: worth_attention=false"
                    )
                score = DealScore.FAIR

        # Calculate discount percentage and dollar savings from VLM estimates.
        #
        # Design principle: each layer does what it's good at.
        #   - VLM evaluates deal quality from images + description (can read
        #     the price directly from the listing photo even if we couldn't scrape it)
        #   - Programmatic savings enforcement overrides VLM ONLY when we have
        #     a confirmed listing price to calculate against
        #   - When listing.price is None, the VLM's assessment stands — it has
        #     more context than our scraper (images, description, seller notes)
        #
        # CRITICAL: listing.price=None means we couldn't extract the price —
        # this is NOT a free item. We set discount/savings to 0 but let the
        # VLM score drive the deal quality.
        has_price = listing.price is not None
        listing_price = listing.price if has_price else 0
        discount_pct = 0.0
        dollar_savings = 0.0

        # Detect free items: price=0 (extracted) OR title says "FREE"/"$0"
        title_lower = (listing.title or "").strip().lower()
        is_free_item = (
            (has_price and listing_price == 0)
            or (not has_price and title_lower in {"free", "$0", "0"})
        )

        # PUBLIC-only value-multiple sanity cap: clamp an implausibly-high VLM
        # value on an unbranded commodity (e.g. "$20 toaster" -> "$150") so a
        # hallucinated multiple cannot mint a deal. Skipped for: branded items,
        # high-value categories, and evidence-backed multi-sample non-retail
        # comps. Watchlist matches are exempt. (A single retail/MSRP hit does
        # NOT defeat the cap — that is the common toaster-class hallucination.)
        if (
            watchlist_context is None
            and has_price
            and listing_price >= 5.0
            and vlm.estimated_value_mid > listing_price * self._max_value_multiple
            and _listing_has_no_brand(listing)
            and not (vlm_identified_brand and vlm.confidence >= 0.7)
            and not _HIGH_VALUE_UNBRANDED_KEYWORDS.search(listing.title or "")
            and not (
                comparables is not None
                and getattr(comparables, "sample_count", 0) > 1
                and getattr(comparables, "source", "") != "retail"
            )
        ):
            clamped = listing_price * self._max_value_multiple
            if provenance:
                provenance.score_adjustments.append(
                    f"value-multiple cap: ${vlm.estimated_value_mid:.0f}->${clamped:.0f}"
                )
            vlm.estimated_value_mid = clamped
            vlm.estimated_value_high = min(vlm.estimated_value_high or clamped, clamped * 1.5)

        if vlm.estimated_value_mid > 0 and listing_price > 0:
            dollar_savings = vlm.estimated_value_mid - listing_price
            discount_pct = (dollar_savings / vlm.estimated_value_mid) * 100
            discount_pct = max(discount_pct, 0.0)
            dollar_savings = max(dollar_savings, 0.0)
        elif is_free_item and vlm.estimated_value_mid > 0:
            # Free item with identified value — 100% savings by definition
            discount_pct = 100.0
            dollar_savings = vlm.estimated_value_mid
        elif not has_price:
            # Price unknown — can't calculate programmatic savings.
            # The VLM's deal_quality assessment stands on its own since
            # the VLM can read the price from the listing image/description.
            log.debug(
                "Price unknown — VLM assessment will drive deal quality",
                title=listing.title[:40],
                external_id=listing.external_id,
            )

        # Sanity check: if VLM says market value <= listing price, can't be a deal
        # (only when we have a confirmed price to compare)
        if listing_price > 0 and vlm.estimated_value_mid > 0:
            if vlm.estimated_value_mid <= listing_price:
                score = DealScore.FAIR
                vlm.deal_quality = "pass"

        # Hard dollar savings enforcement — VLMs consistently hallucinate
        # "incredible" on cheap items with high percentage discounts.
        # These thresholds are NON-NEGOTIABLE and override VLM output.
        # EXCEPTION: when listing.price is None, we can't calculate savings
        # so we trust the VLM's holistic assessment (it sees the actual
        # listing image which shows the price).
        pre_enforcement_score = score
        if is_free_item and vlm.estimated_value_mid > 0:
            # FREE items. Watchlist matches keep their VLM score (gated by their
            # own per-item threshold). On the PUBLIC path a free item must clear
            # a value floor AND be an identifiable resaleable product — not
            # bulk/scrap/consumables — to reach the top tiers (downgrade-only,
            # never blocked). Keeps free treadmill/washer/ice-machine INCREDIBLE
            # while dropping free bricks/candle-supplies/keycaps.
            if watchlist_context is not None:
                if provenance:
                    provenance.score_adjustments.append(
                        f"free item (watchlist): value=${vlm.estimated_value_mid:.0f}, preserved"
                    )
            else:
                value_high = vlm.estimated_value_high or vlm.estimated_value_mid
                non_resaleable = bool(_FREE_ITEM_NON_RESALEABLE.search(listing.title or ""))
                identifiable = (
                    bool(_HIGH_VALUE_UNBRANDED_KEYWORDS.search(listing.title or ""))
                    or not _listing_has_no_brand(listing)
                    or (vlm_identified_brand and vlm.confidence >= 0.7)
                )
                if non_resaleable or value_high < self._free_min_value:
                    score = DealScore.FAIR
                    adj = ("free non-resaleable -> fair" if non_resaleable
                           else f"free value ${value_high:.0f}<${self._free_min_value:.0f} -> fair")
                elif (
                    value_high < self._free_incredible_min_value or not identifiable
                ):
                    if _SCORE_RANK.get(score, 0) > _SCORE_RANK[DealScore.GREAT]:
                        score = DealScore.GREAT
                    adj = ("free unidentifiable -> cap great" if identifiable is False
                           else f"free value ${value_high:.0f}<${self._free_incredible_min_value:.0f} -> cap great")
                else:
                    adj = f"free item: value=${value_high:.0f}, score preserved"
                if provenance:
                    provenance.score_adjustments.append(adj)
        elif has_price:
            score = _enforce_dollar_savings(
                score, discount_pct, dollar_savings,
                listing_price=listing_price,
                is_public=(watchlist_context is None),
                incredible_floor=self._incredible_abs_floor,
            )
            if score != pre_enforcement_score and provenance:
                provenance.score_adjustments.append(
                    f"{pre_enforcement_score.value}->{score.value}: "
                    f"${dollar_savings:.0f} saved, {discount_pct:.0f}%"
                )
        else:
            # VLM score stands, but cap at GREAT for unknown-price listings
            # to prevent "incredible" spam when we can't verify savings.
            if score == DealScore.INCREDIBLE:
                score = DealScore.GREAT
                log.info(
                    "Unknown price — capping VLM score at GREAT",
                    title=listing.title[:40],
                    vlm_quality=vlm.deal_quality,
                )
                if provenance:
                    provenance.score_adjustments.append(
                        "incredible->great: unknown price cap"
                    )

        # Upward promotion: if the VLM scored at least GOOD (not hallucinating
        # — it saw real value) but the savings clearly qualify for a higher tier,
        # promote. This corrects VLM conservatism on objectively great deals
        # (e.g., $25 oak cabinet worth $150 = 83% off, $125 savings → incredible
        # by any standard, but VLM only said "great").
        # Promotion requires: real price, $15+ listing price (cheap items
        # stay at VLM score), and VLM scored GOOD or GREAT. Uses FLAT dollar
        # thresholds ($30 for great, $75 for incredible) — no proportional
        # scaling, because promotion is an upgrade that needs hard evidence.
        if has_price and listing_price >= 15.0 and score in (DealScore.GOOD, DealScore.GREAT):
            for promo_tier in (DealScore.INCREDIBLE, DealScore.GREAT):
                if _SCORE_RANK[promo_tier] <= _SCORE_RANK[score]:
                    continue  # Can't promote to same or lower tier
                min_dollars = _MIN_DOLLAR_SAVINGS[promo_tier]  # Flat: $30 or $75
                min_pct = _MIN_DISCOUNT_PCT.get(promo_tier, 0.0)
                if dollar_savings >= min_dollars and discount_pct >= min_pct:
                    log.info(
                        "Deal promoted by savings evidence",
                        original=score.value,
                        promoted_to=promo_tier.value,
                        dollar_savings=round(dollar_savings, 2),
                        discount_pct=round(discount_pct, 1),
                    )
                    if provenance:
                        provenance.score_adjustments.append(
                            f"{score.value}->{promo_tier.value}: "
                            f"${dollar_savings:.0f} saved, {discount_pct:.0f}% (promotion)"
                        )
                    score = promo_tier
                    break  # Take the highest qualifying tier

        if provenance:
            provenance.final_score = score.value

        # Check minimum score threshold. A watchlist match is gated by the
        # USER's per-item notification_threshold (often 'all' = notify on any
        # confirmed match), NOT the global deal-quality floor — they're watching
        # a specific item, not hunting bargains. Non-watch listings use the
        # global min. See docs/decisions/watchlist-honors-threshold-not-freshness.md.
        interest = watchlist_context.get("interest", "") if watchlist_context else ""
        gate_score = self._min_score
        if watchlist_context:
            gate_score = _WATCH_THRESHOLD_TO_SCORE.get(
                str(watchlist_context.get("threshold") or "good").lower(),
                self._min_score,
            )
        if _SCORE_RANK.get(score, 0) < _SCORE_RANK.get(gate_score, 0):
            log.info(
                "listing.rejected",
                title=listing.title[:60],
                price=listing_price if has_price else None,
                vlm_score=vlm.deal_quality,
                final_score=score.value,
                reason="below_min_threshold",
                min_required=gate_score.value,
                dollar_savings=round(dollar_savings, 2) if has_price else None,
                discount_pct=round(discount_pct, 1) if has_price else None,
                interest=interest or None,
            )
            return None

        # Build reasoning — strip any [Web: ...] blocks (defense-in-depth)
        reasoning = re.sub(r"\s*\[Web:.*?\]", "", vlm.reasoning).strip()
        if vlm.red_flags:
            reasoning += f" Flags: {', '.join(vlm.red_flags)}."

        deal = Deal(
            listing_id=listing.id or "",
            score=score,
            estimated_market_price=vlm.estimated_value_mid,
            discount_pct=round(discount_pct, 1),
            llm_reasoning=reasoning,
            provenance_json=provenance.to_json() if provenance else "",
        )

        # Attach watchlist item ID if this is a watchlist match
        if watchlist_context:
            deal.watch_item_id = watchlist_context.get("watch_item_id")

        log.info(
            "listing.deal_found",
            title=listing.title[:60],
            price=listing_price if has_price else None,
            score=score.value,
            vlm_score=vlm.deal_quality,
            dollar_savings=round(dollar_savings, 2) if has_price else None,
            discount_pct=round(discount_pct, 1) if has_price else None,
            market_value=round(vlm.estimated_value_mid, 2) if vlm.estimated_value_mid else None,
            interest=interest or None,
            is_watchlist=bool(watchlist_context),
        )

        return deal


# --- Hard programmatic filters (VLM cannot override these) ---

# Category keywords for higher unbranded caps.  Even without a brand, a 55" TV
# or a washer/dryer is legitimately worth more than $100.
_HIGH_VALUE_UNBRANDED_KEYWORDS = re.compile(
    r"\b(?:"
    r"tv|television|monitor|plasma|oled|qled|lcd"
    r"|laptop|computer|desktop|tablet|ipad"
    r"|washer|dryer|refrigerator|fridge|freezer|dishwasher|oven|range|stove"
    r"|treadmill|elliptical|exercise bike|rowing machine"
    r"|couch|sofa|sectional|recliner|mattress"
    r"|snowblower|snow blower|lawn mower|mower|generator"
    r"|espresso machine|espresso maker"
    r"|table saw|band saw|bandsaw|planer|jointer|lathe|drill press"
    r"|miter saw|mitre saw|scroll saw|router table|dovetail jig"
    r"|welder|air compressor|pressure washer"
    r"|ice machine|ice maker|chest freezer|kegerator|kayak|canoe"
    r"|grill|smoker|sewing machine|piano|keyboard|drum kit|drum set"
    r")\b",
    re.IGNORECASE,
)

_DEFAULT_UNBRANDED_CAP = 100.0
_HIGH_VALUE_UNBRANDED_CAP = 500.0


def _get_unbranded_value_cap(title: str | None) -> float:
    """Return the unbranded value cap based on the item category.

    High-value categories (TVs, appliances, furniture, exercise equipment)
    get a $500 cap instead of $100 since even off-brand items in these
    categories can legitimately exceed $100.
    """
    if title and _HIGH_VALUE_UNBRANDED_KEYWORDS.search(title):
        return _HIGH_VALUE_UNBRANDED_CAP
    return _DEFAULT_UNBRANDED_CAP


# Common marketplace brands — items mentioning these are "branded"
# and NOT subject to the unbranded value cap.  Lowercase.
_KNOWN_BRANDS = frozenset({
    # --- Tech & Electronics ---
    "apple", "samsung", "sony", "lg", "dell", "hp", "lenovo", "asus", "acer",
    "microsoft", "google", "nintendo", "xbox", "playstation", "bose", "jbl",
    "sonos", "sennheiser", "audio-technica", "beats", "marshall", "harman kardon",
    "bang & olufsen", "b&o", "anker", "logitech", "corsair", "razer",
    # --- PC Components ---
    "kingston", "g.skill", "gskill", "crucial", "western digital", "wd",
    "seagate", "evga", "msi", "gigabyte", "amd", "intel", "nvidia",
    "sapphire", "zotac", "pny", "teamgroup", "team group", "patriot",
    "noctua", "be quiet", "cooler master", "thermaltake",
    "rtx", "gtx", "radeon", "ryzen", "threadripper",
    # --- Kitchen Appliances ---
    "kitchenaid", "kitchen aid", "cuisinart", "ninja", "instant pot", "dyson",
    "roomba", "irobot", "breville", "vitamix", "keurig", "nespresso",
    "le creuset", "lodge", "all-clad", "calphalon", "oster", "hamilton beach",
    "pampered chef", "chefman",
    # --- Furniture & Home ---
    "herman miller", "steelcase", "ikea", "pottery barn", "west elm",
    "restoration hardware", "crate and barrel", "wayfair",
    # --- Collectibles & Ceramics ---
    "rae dunn", "fiesta", "fiestaware", "pyrex", "corningware", "corning ware",
    "mccoy", "pfaltzgraff", "lenox", "wedgwood", "waterford", "swarovski",
    "hummel", "precious moments", "dept 56", "department 56",
    # --- Vintage & Specialty ---
    "west bend", "scentsy", "tupperware",
    # --- Tools & Outdoor ---
    "dewalt", "milwaukee", "makita", "bosch", "ryobi", "craftsman", "snap-on",
    "porter cable", "porter-cable", "rikon", "delta", "jet", "grizzly",
    "festool", "ridgid", "kobalt", "hitachi", "metabo", "dremel",
    "black and decker", "black & decker", "worx", "ego", "greenworks",
    "stihl", "husqvarna", "honda", "toyota", "john deere", "weber",
    "traeger", "blackstone", "yeti", "hydro flask", "stanley",
    # --- Apparel & Sports ---
    "nike", "adidas", "under armour", "lululemon", "patagonia", "north face",
    # --- Cameras & Drones ---
    "canon", "nikon", "fujifilm", "gopro", "dji",
    # --- Major Appliances ---
    "whirlpool", "maytag", "ge", "frigidaire", "kenmore", "bosch",
    "carrier", "trane", "nest", "ring", "simplisafe",
    # --- Fitness ---
    "peloton", "bowflex", "rogue", "concept2",
    # --- Toys & Games ---
    "lego", "hot wheels", "barbie", "nerf",
    "pokemon", "magic the gathering", "mtg",
    "fisher price", "fisher-price", "mattel", "hasbro", "playskool",
    "little tikes", "playmobil", "melissa & doug", "melissa and doug",
    "step2", "vtech", "leapfrog", "razor", "radio flyer",
    "funko", "squishmallow", "american girl", "build-a-bear",
    "transformers", "gi joe", "tonka", "play-doh",
    # --- Bikes ---
    "trek", "specialized", "giant", "cannondale",
    # --- Musical Instruments ---
    "fender", "gibson", "yamaha", "roland", "casio",
    # --- Product lines that unmistakably indicate branded items ---
    "ps5", "ps4", "ps3", "xbox", "wii", "switch",
    "iphone", "ipad", "macbook", "imac", "airpods",
    "kindle", "alexa", "echo dot", "fire tv",
    "roomba", "cricut", "instax",
    "keurig", "nespresso", "vitamix", "instant pot",
})


def _listing_has_no_brand(listing: Listing) -> bool:
    """Check if a listing has no identifiable brand in title or description.

    Returns True if the listing appears to be a generic, unbranded item
    (e.g., "Office chair", "Baby dish set").
    """
    text = f"{listing.title or ''} {listing.description or ''}".lower()
    # Also check structured condition/brand metadata
    raw = listing.raw_data or {}
    brand_meta = str(raw.get("brand", "")).lower()
    if brand_meta and brand_meta not in {"unknown", "unbranded", "generic", "other"}:
        return False

    for brand in _KNOWN_BRANDS:
        if brand in text:
            return False
    return True


def _vlm_identified_known_brand(item_identified: str) -> str | None:
    """Check if the VLM's item_identified field contains a known brand.

    Returns the brand name if found, None otherwise. This lets us bypass
    the unbranded value cap when the VLM can see a brand in the photos
    that wasn't in the listing text.
    """
    if not item_identified:
        return None
    text = item_identified.lower()
    for brand in _KNOWN_BRANDS:
        if brand in text:
            return brand
    return None


# Minimum dollar savings required per deal score tier.
# VLMs consistently rate cheap junk as "incredible" because the percentage
# is high (a $1 spice rack at $10 value = 90% off). But saving $9 is NOT
# incredible. These thresholds enforce real-world significance.
#
# For cheap items (listing_price < $30), flat dollar thresholds are too
# aggressive — a $20 item at 50% off ($10 savings) is legitimately great
# but would fail the $30 GREAT threshold. So we scale dollar thresholds
# proportionally: min_dollars = max(flat_threshold, pct_of_price * listing_price).
# Below $30 the percentage-of-price dominates; above $50 the flat threshold does.
_MIN_DOLLAR_SAVINGS: dict[DealScore, float] = {
    DealScore.GOOD: 10.0,        # Must save at least $10
    DealScore.GREAT: 30.0,       # Must save at least $30
    DealScore.INCREDIBLE: 75.0,  # Must save at least $75
}

# For cheap items, use percentage-of-listing-price instead of flat dollar minimum.
# This prevents over-penalizing budget items while still catching junk.
_MIN_SAVINGS_PCT_OF_PRICE: dict[DealScore, float] = {
    DealScore.GOOD: 0.15,        # Save at least 15% of listing price
    DealScore.GREAT: 0.25,       # Save at least 25% of listing price
    DealScore.INCREDIBLE: 0.40,  # Save at least 40% of listing price
}

# Minimum percentage discount per tier (in addition to dollar savings).
_MIN_DISCOUNT_PCT: dict[DealScore, float] = {
    DealScore.GOOD: 15.0,
    DealScore.GREAT: 30.0,
    DealScore.INCREDIBLE: 50.0,
}

# PUBLIC-ONLY selectivity floors (watchlist matches are exempt — gated by their
# own per-item notification_threshold). See
# docs/decisions/public-incredible-selectivity-floors.md.
#
# Absolute dollar floor the proportional <$50 path can NEVER go below: even a
# 100%-off cheap item needs real absolute savings to reach a tier. This is what
# demotes "$7->$18 waffle pan" ($11 saved) and "shot glasses $1->$3" from
# INCREDIBLE. The INCREDIBLE value is operator-tunable via config.
_ABS_DOLLAR_FLOOR: dict[DealScore, float] = {
    DealScore.GOOD: 8.0,
    DealScore.GREAT: 20.0,
    DealScore.INCREDIBLE: 50.0,
}

# Free items whose title names a non-resaleable bulk/consumable good are not
# deals regardless of the VLM's value guess (kills "free bricks->$250",
# "free candle supplies", "mens clothes"). PHRASE-matched to avoid collisions
# ("boxes" vs "box spring"). High-value free items (treadmill, washer) are
# identified by _HIGH_VALUE_UNBRANDED_KEYWORDS instead and pass.
_FREE_ITEM_NON_RESALEABLE = re.compile(
    r"\b(?:bricks?|fire\s*wood|firewood|mulch|top\s*soil|topsoil|gravel|sand"
    r"|scrap(?:\s+metal)?|moving\s+boxes|cardboard|packing\s+material"
    r"|candle(?:\s+making)?\s+supplies|craft\s+supplies"
    r"|wax\s+(?:bars?|melts?|pods?|cubes?)"
    r"|mens?\s+clothes|womens?\s+clothes|kids?\s+clothes|clothing\s+lot"
    r"|free\s+stuff|free\s+junk)\b",
    re.IGNORECASE,
)


def _get_effective_min_dollars(
    tier: DealScore, listing_price: float, *,
    is_public: bool = False, incredible_floor: float | None = None,
) -> float:
    """Get the effective minimum dollar savings for a tier, scaled by price.

    For expensive items ($50+), uses the flat threshold (e.g., $10 for GOOD).
    For cheap items (< $30), uses a percentage of listing price, which is
    lower and avoids over-penalizing budget items. Between $30-$50, blends
    smoothly between the two.

    On the PUBLIC path (``is_public``) an absolute dollar floor is applied that
    the proportional cheap-item path can never go below — so a 100%-off $7 item
    cannot reach INCREDIBLE on a trivial $11 saving. Watchlist matches pass
    ``is_public=False`` and are unaffected.

    Args:
        tier: Deal score tier.
        listing_price: The listing's asking price.
        is_public: Whether this is the public feed (apply the absolute floor).
        incredible_floor: Operator-tuned absolute floor for INCREDIBLE.

    Returns:
        Effective minimum dollar savings.
    """
    flat_min = _MIN_DOLLAR_SAVINGS.get(tier, 0.0)

    if listing_price <= 0:
        eff = flat_min
    else:
        pct_of_price = _MIN_SAVINGS_PCT_OF_PRICE.get(tier, 0.0)
        # For items under $50, the proportional threshold may be lower than flat.
        proportional_min = listing_price * pct_of_price
        # Use the LOWER of the two — generous to cheap items, while still
        # enforcing flat thresholds on expensive items.
        eff = min(flat_min, proportional_min) if listing_price < 50.0 else flat_min

    if is_public:
        floor = _ABS_DOLLAR_FLOOR.get(tier, 0.0)
        if tier == DealScore.INCREDIBLE and incredible_floor is not None:
            floor = incredible_floor
        eff = max(eff, floor)
    return eff


def _enforce_dollar_savings(
    score: DealScore, discount_pct: float, dollar_savings: float,
    listing_price: float = 0.0, *,
    is_public: bool = False, incredible_floor: float | None = None,
) -> DealScore:
    """Downgrade deal score if dollar savings or percentage don't meet minimums.

    The VLM assigns deal quality based on its assessment, but it frequently
    overrates cheap items. This function enforces hard numerical thresholds
    that cannot be hallucinated away.

    For cheap items (< $50), dollar thresholds scale proportionally with
    listing price so that budget deals aren't unfairly penalized. A $20 item
    at 50% off ($10 saved) should qualify as GOOD even though $10 < $30.

    Args:
        score: VLM-assigned deal score.
        discount_pct: Calculated discount percentage.
        dollar_savings: Actual dollar amount saved.
        listing_price: The listing's asking price (for scaling thresholds).

    Returns:
        Potentially downgraded DealScore.
    """
    if score == DealScore.FAIR or score == DealScore.UNKNOWN:
        return score

    # Walk down from the VLM's rating until we find a tier the savings qualify for
    tiers = [DealScore.INCREDIBLE, DealScore.GREAT, DealScore.GOOD, DealScore.FAIR]
    current_rank = _SCORE_RANK.get(score, 0)

    for tier in tiers:
        tier_rank = _SCORE_RANK.get(tier, 0)
        if tier_rank > current_rank:
            continue  # Skip tiers above the VLM's rating

        min_dollars = _get_effective_min_dollars(
            tier, listing_price, is_public=is_public, incredible_floor=incredible_floor,
        )
        min_pct = _MIN_DISCOUNT_PCT.get(tier, 0.0)

        if dollar_savings >= min_dollars and discount_pct >= min_pct:
            if tier_rank < current_rank:
                log.info(
                    "Deal downgraded by savings enforcement",
                    original=score.value,
                    downgraded_to=tier.value,
                    dollar_savings=round(dollar_savings, 2),
                    discount_pct=round(discount_pct, 1),
                    min_dollars_required=round(min_dollars, 2),
                    listing_price=round(listing_price, 2),
                )
            return tier

    # Doesn't meet even GOOD thresholds
    if current_rank > _SCORE_RANK[DealScore.FAIR]:
        min_good_dollars = _get_effective_min_dollars(
            DealScore.GOOD, listing_price, is_public=is_public, incredible_floor=incredible_floor,
        )
        log.info(
            "Deal downgraded to FAIR by savings enforcement",
            original=score.value,
            dollar_savings=round(dollar_savings, 2),
            discount_pct=round(discount_pct, 1),
            min_good_dollars=round(min_good_dollars, 2),
            listing_price=round(listing_price, 2),
        )
    return DealScore.FAIR


# Patterns that indicate a listing is misleading (not actually for sale,
# trades only, popup shop, bait pricing).
_MISLEADING_PATTERNS = [
    # Trades / not actually selling
    (r"\b(?:trade|trades|trading)\s*(?:only|preferred|wanted)\b", "trades_only"),
    (r"\bno\s+cash\b", "no_cash"),
    (r"\blooking\s+to\s+trade\b", "looking_to_trade"),
    # Popup shops / events (not actual marketplace listings)
    (r"\bpop\s*-?\s*up\b", "popup_event"),
    (r"\bvendor\s+event\b", "vendor_event"),
    (r"\bcraft\s+(?:fair|show)\b", "craft_fair"),
    # Bait pricing: listed free/cheap but description says otherwise
    (r"\bmake\s+(?:an?\s+)?offer\b", None),  # Only suspicious if price is $0
    (r"\bstarting\s+(?:at|bid)\b", None),  # Only suspicious if price is $0
]


def _detect_misleading_listing(listing: Listing) -> str | None:
    """Detect listings that aren't genuine sales (trades, popups, bait pricing).

    Args:
        listing: The marketplace listing.

    Returns:
        Reason string if misleading, None if legitimate.
    """
    desc = (listing.description or "").lower()
    title = listing.title.lower()
    text = f"{title} {desc}"
    listing_price = listing.price or 0

    for pattern, reason in _MISLEADING_PATTERNS:
        if re.search(pattern, text):
            if reason is None:
                # Conditional patterns — only flag if price suggests bait
                if listing_price <= 0:
                    return "bait_pricing"
            else:
                return reason

    # $0 listing with description mentioning prices/payments = bait
    if listing_price <= 0 and desc:
        price_mentions = re.findall(r"\$\d+", desc)
        if len(price_mentions) >= 2:
            return "hidden_pricing"

    return None


# Replacement parts / repair components filter.
# When a user watches "kitchen aid" or "KitchenAid", they want the actual
# product (mixer, blender, etc.) — NOT $5 gaskets, valves, or seals that
# happen to mention the brand. These waste VLM budget and produce spam
# notifications that erode user trust.
#
# Detection strategy: part numbers (alphanumeric codes like W10830274) OR
# repair-part keywords in title. Only applied to watchlist listings where
# the interest is a brand/product name (not explicitly a "part").
_PART_NUMBER_PATTERN = re.compile(
    r"\b[A-Z]{1,3}\d{5,}[A-Z]?\d*\b",  # e.g., W10830274, AP6872729, PS12585482
    re.IGNORECASE,
)

_REPLACEMENT_PART_KEYWORDS = re.compile(
    r"\b(?:"
    r"gasket|valve|seal|hose|element|thermostat|sensor|switch"
    r"|bracket|clip|latch|hinge|handle|knob|shaft|bearing"
    r"|pump|motor|belt|drum|agitator|impeller|auger"
    r"|board|control\s+board|circuit\s+board|pcb"
    r"|filter\s+(?:cartridge|replacement)|water\s+filter"
    r"|door\s+(?:gasket|seal|latch|switch|hinge)"
    r"|replacement\s+part|repair\s+part|oem\s+part"
    r"|compatible\s+(?:with|for)|fits\s+(?:model|whirlpool|kenmore|maytag|lg|samsung|ge)"
    r"|for\s+(?:whirlpool|kenmore|maytag|lg|samsung|ge)\b"
    r")\b",
    re.IGNORECASE,
)

# Interests that are explicitly looking for parts (don't filter these)
_PART_INTEREST_KEYWORDS = re.compile(
    r"\b(?:part|gasket|valve|filter|seal|element|replacement|repair)\b",
    re.IGNORECASE,
)


def _is_replacement_part(listing: Listing, interest: str) -> bool:
    """Check if a listing is a replacement part for a brand the user is watching.

    Returns True if the listing looks like a repair part (gasket, valve, etc.)
    and the user's interest is for the actual product, not the part.
    """
    # No interest = base browse listing, not watchlist — don't filter
    if not interest:
        return False

    # If user explicitly wants parts, don't filter
    if _PART_INTEREST_KEYWORDS.search(interest):
        return False

    title = listing.title
    text = f"{title} {listing.description or ''}".strip()

    # Check for part numbers in title (strong signal)
    if _PART_NUMBER_PATTERN.search(title):
        return True

    # Check for repair-part keywords in title + description
    if _REPLACEMENT_PART_KEYWORDS.search(text):
        return True

    return False


def _pre_enrichment_preference_filter(
    items: list[tuple[Listing, TriageResult]],
    watchlist_items: list[WatchItem],
) -> list[tuple[Listing, TriageResult]]:
    """Filter out listings that contradict user preferences BEFORE enrichment.

    Runs the same programmatic checks as the post-VLM preference filter, but
    at zero cost — catches "only cups" vs "vase", bulk constraints, negation
    patterns, and replacement parts before burning Vision API calls and VLM
    evaluations.
    """
    watch_map: dict[str, WatchItem] = {w.id: w for w in watchlist_items if w.id}
    kept: list[tuple[Listing, TriageResult]] = []

    for listing, triage in items:
        watch_id = listing.raw_data.get("_watch_item_id")
        notes = ""
        interest = ""
        if watch_id and watch_id in watch_map:
            notes = watch_map[watch_id].notes or ""
            interest = watch_map[watch_id].interest or ""

        # Replacement parts filter: reject gaskets/valves/seals when user
        # wants the actual product (e.g., "kitchen aid" not "kitchen aid gasket")
        if interest and _is_replacement_part(listing, interest):
            log.info(
                "Pre-enrichment preference reject",
                title=listing.title[:50],
                excluded_term="replacement_part",
                interest=interest,
            )
            continue

        if notes:
            contradiction = _listing_contradicts_notes(listing, notes)
            if contradiction:
                log.info(
                    "Pre-enrichment preference reject",
                    title=listing.title[:50],
                    excluded_term=contradiction,
                    notes=notes[:80],
                )
                continue

        kept.append((listing, triage))

    return kept


# Titles that indicate DOM extraction captured only the freshness badge,
# not the actual listing title.  These have zero signal for text triage.
_GARBAGE_TITLES = frozenset({
    "just listed",
    "listed today",
    "listed yesterday",
    "new listing",
    "",
})

# Minimum meaningful text length (title + description combined) for triage
# to add value.  Below this, VLM with image access is the only useful evaluator.
_MIN_TRIAGE_TEXT_LENGTH = 8


def _should_bypass_triage(listing: Listing) -> str | None:
    """Determine if a listing should skip text triage and go straight to VLM.

    Returns a reason string if triage should be bypassed, None otherwise.

    Bypasses in two cases:
    1. Low-quality text (garbage titles, near-empty text) — VLM with image
       access is the only useful evaluator.
    2. Watchlist-sourced listings (tagged with ``_watch_item_id``) — the user
       explicitly asked for these items. The triage LLM is unreliable at
       recognizing watchlist matches (observed 77% kill rate). Preference
       constraints (negation, bulk, "only X") are enforced programmatically
       by ``_pre_enrichment_preference_filter`` after this bypass, so no
       constraint enforcement is lost.
    """
    # Watchlist-sourced listings bypass triage — user explicitly wants them
    if listing.raw_data.get("_watch_item_id"):
        return "watchlist_tagged"

    # Low-quality text extraction
    title_clean = listing.title.strip().lower()
    # Remove "listed X ago" patterns for check
    title_clean = re.sub(r"listed\s+\d+\s*[hmd]\w*\s+ago", "", title_clean).strip()

    if title_clean in _GARBAGE_TITLES:
        return "garbage_title"

    # Very short title with no description — triage can't assess
    description = (listing.description or "").strip()
    combined_len = len(listing.title.strip()) + len(description)
    if combined_len < _MIN_TRIAGE_TEXT_LENGTH:
        return "insufficient_text"

    return None


_NEGATION_PATTERN = re.compile(
    r"\b(?:not?|avoid|never|without|don'?t\s+want)\s+(.+?)(?:[,.\n;]|$)",
    re.IGNORECASE,
)

# Synonym expansion for positive constraints ("only cups" should accept tumblers, etc.)
# Keys are the exact extracted term; values are additional words that satisfy it.
_REQUIRED_TERM_SYNONYMS: dict[str, list[str]] = {
    "cups": ["cup", "mug", "mugs", "tumbler", "tumblers", "glass", "glasses",
             "goblet", "goblets", "flute", "flutes", "beaker", "stein",
             "drinkware", "glassware", "creamer"],
    "cup": ["cups", "mug", "mugs", "tumbler", "tumblers", "glass", "glasses",
            "flute", "flutes", "beaker", "stein", "drinkware"],
    "mugs": ["mug", "cup", "cups", "tumbler", "tumblers", "stein"],
    "mug": ["mugs", "cup", "cups", "tumbler", "tumblers", "stein"],
    "glasses": ["glass", "cup", "cups", "tumbler", "tumblers", "flute", "goblet"],
    "wood": ["wooden", "oak", "maple", "pine", "walnut", "birch", "bamboo", "teak"],
    "wooden": ["wood", "oak", "maple", "pine", "walnut", "birch", "bamboo"],
    "metal": ["steel", "aluminum", "aluminium", "iron", "stainless", "chrome"],
    "ceramic": ["porcelain", "stoneware", "clay", "pottery"],
}

# Positive constraint patterns: "only cups", "must be wood", "prefer ceramic"
_POSITIVE_CONSTRAINT_PATTERN = re.compile(
    r"\b(?:only|must\s+be|must\s+have|exclusively|strictly)\s+(.+?)(?:[,.\n;]|$)",
    re.IGNORECASE,
)

# Numeric minimum constraints: "minimum 50"", "min 50 inches", "at least 8gb"
_NUMERIC_MIN_PATTERN = re.compile(
    r"\b(?:minimum|min|at\s+least)\s+(\d+(?:\.\d+)?)\s*([\"″''ʺ]|inch(?:es)?|gb|tb|lb|lbs|oz|qt|gal|cu\.?\s*ft)?",
    re.IGNORECASE,
)

# Numeric maximum constraints: "maximum 40lbs", "max 30 inches", "no more than 50"
_NUMERIC_MAX_PATTERN = re.compile(
    r"\b(?:maximum|max|no\s+more\s+than|under|below)\s+(\d+(?:\.\d+)?)\s*([\"″''ʺ]|inch(?:es)?|gb|tb|lb|lbs|oz|qt|gal|cu\.?\s*ft)?",
    re.IGNORECASE,
)

# Extract numbers with optional unit suffixes from listing text.
# Matches: 33", 50 inch, 65-inch, 8gb, 55", 12 oz, etc.
_LISTING_NUMBER_PATTERN = re.compile(
    r"(\d+(?:\.\d+)?)\s*[-]?\s*([\"″''ʺ]|inch(?:es)?|gb|tb|lb|lbs|oz|qt|gal|cu\.?\s*ft)",
    re.IGNORECASE,
)

# Unit normalization: collapse synonyms into canonical forms
_UNIT_CANONICAL: dict[str, str] = {
    '"': "inch", "″": "inch", "'": "inch", "'": "inch", "ʺ": "inch",
    "inches": "inch", "inch": "inch",
    "gb": "gb", "tb": "tb",
    "lb": "lb", "lbs": "lb",
    "oz": "oz", "qt": "qt", "gal": "gal",
}


def _normalize_unit(raw: str | None) -> str:
    """Normalize a unit string to a canonical form for comparison."""
    if not raw:
        return ""
    raw = raw.strip().lower().rstrip(".")
    # Handle "cu ft" / "cu. ft" → "cu ft"
    raw = re.sub(r"cu\.?\s*ft", "cu ft", raw)
    return _UNIT_CANONICAL.get(raw, raw)


def _extract_numeric_constraints(
    notes: str,
) -> list[tuple[str, float, str]]:
    """Extract numeric min/max constraints from user notes.

    Returns list of (direction, value, unit) tuples, e.g.:
    - ("min", 50.0, "inch") from "minimum 50\" size"
    - ("max", 40.0, "lb") from "max 40lbs"
    """
    constraints: list[tuple[str, float, str]] = []
    for m in _NUMERIC_MIN_PATTERN.finditer(notes):
        val = float(m.group(1))
        unit = _normalize_unit(m.group(2))
        constraints.append(("min", val, unit))
    for m in _NUMERIC_MAX_PATTERN.finditer(notes):
        val = float(m.group(1))
        unit = _normalize_unit(m.group(2))
        constraints.append(("max", val, unit))
    return constraints


def _listing_violates_numeric_constraint(
    listing: Listing,
    constraints: list[tuple[str, float, str]],
) -> str | None:
    """Check if a listing violates any numeric min/max constraint.

    Extracts all numbers-with-units from the listing title + description,
    then checks each constraint. Returns a description of the violation
    or None if no violation.
    """
    if not constraints:
        return None

    text = f"{listing.title} {listing.description or ''}"
    # Extract all (value, unit) pairs from listing text
    listing_values: list[tuple[float, str]] = []
    for m in _LISTING_NUMBER_PATTERN.finditer(text):
        val = float(m.group(1))
        unit = _normalize_unit(m.group(2))
        listing_values.append((val, unit))

    for direction, threshold, constraint_unit in constraints:
        # Find listing values that match the same unit
        matching = [v for v, u in listing_values if u == constraint_unit]
        if not matching:
            # No matching dimension found in listing — can't enforce
            continue
        # Use the largest value for "min" checks, smallest for "max"
        if direction == "min":
            best = max(matching)
            if best < threshold:
                return f"below_minimum:{best}<{threshold}{constraint_unit}"
        elif direction == "max":
            best = min(matching)
            if best > threshold:
                return f"above_maximum:{best}>{threshold}{constraint_unit}"

    return None


def _extract_excluded_terms(notes: str) -> list[str]:
    """Extract excluded terms from user preference notes.

    Parses patterns like "not metal", "no IKEA", "avoid particle board",
    "without wheels", "don't want plastic".

    Returns:
        List of lowercased excluded terms.
    """
    if not notes:
        return []

    excluded: list[str] = []
    for match in _NEGATION_PATTERN.finditer(notes):
        # Take up to 3 words after the negation keyword
        term = match.group(1).strip().lower()
        words = term.split()[:3]
        excluded.append(" ".join(words))
    return excluded


def _extract_required_terms(notes: str) -> list[str]:
    """Extract positive constraint terms from user preference notes.

    Parses patterns like "only cups", "must be wood", "exclusively ceramic".
    Handles compound terms: "only cups and mugs" → ["cups", "mugs"].
    These are items where AT LEAST ONE must appear in the listing text —
    if none match, the listing is rejected.

    Returns:
        List of lowercased required terms (any one matching = pass).
    """
    if not notes:
        return []

    required: list[str] = []
    for match in _POSITIVE_CONSTRAINT_PATTERN.finditer(notes):
        term = match.group(1).strip().lower()
        # Split on "and", "or", "&", "/" to handle compound constraints
        # "only cups and mugs" → ["cups", "mugs"]
        # "only disc version" → ["disc version"]
        parts = re.split(r"\s+(?:and|or|&|/)\s+", term)
        for part in parts:
            part = part.strip()
            if part:
                # Take up to 3 words per part
                words = part.split()[:3]
                required.append(" ".join(words))
    return required


def _listing_contradicts_notes(
    listing: Listing,
    notes: str,
) -> str | None:
    """Check if a listing contradicts user preference notes.

    Four-way check:
    1. **Negations**: "not metal" → reject if listing contains "metal"
    2. **Positive constraints**: "only cups" → reject if listing does NOT
       contain "cups" (or any of the required terms)
    3. **Bulk constraint**: "bulk" in notes → reject if listing appears to
       be a single item (no lot/bulk/collection signals)
    4. **Numeric constraints**: "minimum 50\"" → reject if listing has a
       smaller number with matching unit (e.g. 33" TV < 50" minimum)

    Returns the contradicting term if found, or None if no contradiction.
    """
    text = f"{listing.title} {listing.description or ''}".lower()

    # Check negations: listing must NOT contain these terms
    excluded = _extract_excluded_terms(notes)
    for term in excluded:
        pattern = r"\b" + re.escape(term) + r"\b"
        if re.search(pattern, text):
            return term

    # Check positive constraints: listing MUST contain at least one
    required = _extract_required_terms(notes)
    if required:
        found_any = False
        for term in required:
            # Expand the required term with known synonyms so "only cups"
            # also accepts "tumbler", "glass", "mug", etc.
            candidates = [term] + _REQUIRED_TERM_SYNONYMS.get(term, [])
            for candidate in candidates:
                pattern = r"\b" + re.escape(candidate) + r"\b"
                if re.search(pattern, text):
                    found_any = True
                    break
            if found_any:
                break
        if not found_any:
            return f"missing_required:{','.join(required)}"

    # Check bulk constraint: if notes mention "bulk", listing must show
    # signs of being a lot/collection, not a single item
    if _notes_require_bulk(notes) and not _listing_is_bulk(text):
        return "not_bulk"

    # Check numeric constraints: "minimum 50"" → reject 33" TVs
    numeric = _extract_numeric_constraints(notes)
    if numeric:
        violation = _listing_violates_numeric_constraint(listing, numeric)
        if violation:
            return violation

    return None


_BULK_NOTE_PATTERN = re.compile(
    r"\b(?:bulk|lot|lots|many\s+\w+|shoebox|collection)\b",
    re.IGNORECASE,
)

_BULK_LISTING_SIGNALS = re.compile(
    r"\b(?:bulk|lot|lots|collection|bundle|set\s+of|box\s+of|"
    r"shoebox|assorted|\d+\s*\+?\s*cards|\d+\s*\+?\s*pieces?|"
    r"huge|massive|stack|pile|grab\s+bag|mixed|variety)\b",
    re.IGNORECASE,
)


def _notes_require_bulk(notes: str) -> bool:
    """Check if user notes indicate they want bulk/lot listings."""
    return bool(_BULK_NOTE_PATTERN.search(notes))


def _listing_is_bulk(text: str) -> bool:
    """Check if a listing appears to be a bulk/lot/collection listing."""
    return bool(_BULK_LISTING_SIGNALS.search(text))


def _build_provenance(
    triage: TriageResult,
    enrichment: Any,
    comparables: Any,
) -> DealProvenance:
    """Create a DealProvenance from triage, enrichment, and price lookup data.

    Uses safe attribute access to handle mocks and missing fields gracefully.
    """
    # Safe extraction — enrichment/comparables may be mocks in tests
    def _str(obj: Any, attr: str, default: str = "") -> str:
        val = getattr(obj, attr, default) if obj else default
        return str(val) if isinstance(val, str) else default

    def _int(obj: Any, attr: str, default: int = 0) -> int:
        val = getattr(obj, attr, default) if obj else default
        return val if isinstance(val, int) else default

    def _float(obj: Any, attr: str, default: float = 0.0) -> float:
        val = getattr(obj, attr, default) if obj else default
        return val if isinstance(val, (int, float)) else default

    return DealProvenance(
        triage_bypass_reason=triage.reasoning if not triage.investigate else "",
        scam_signals=triage.scam_signals if isinstance(triage.scam_signals, list) else [],
        urgency_signals=triage.urgency_signals if isinstance(triage.urgency_signals, list) else [],
        enrichment_tier=_int(enrichment, "enrichment_tier", 3),
        enrichment_product=_str(enrichment, "enriched_product_name"),
        price_source=_str(comparables, "source"),
        price_confidence=_float(comparables, "confidence"),
        price_sample_count=_int(comparables, "sample_count"),
    )


def _build_watchlist_context(
    listing: Listing,
    watchlist_items: list[WatchItem] | None,
) -> dict[str, Any] | None:
    """Check if a listing matches any watchlist item and build context.

    Uses two strategies (in order):
    1. **Origin tag** — if this listing was found via a watchlist keyword search,
       it carries ``_watch_item_id`` in ``raw_data``.  This is the most reliable
       signal because the user explicitly searched for this interest.
    2. **Keyword + synonym matching** — for listings that came from the general
       category sweep, fall back to ``InterestMatcher._interest_matches`` which
       includes synonym expansion.
    """
    if not watchlist_items:
        return None

    # Build a lookup by item ID for the origin-tag path
    items_by_id: dict[str, WatchItem] = {
        item.id: item for item in watchlist_items if item.id
    }

    from poob.scanner.interest_matcher import InterestMatcher
    from poob.utils.content import clean_fb_description

    title_lower = listing.title.lower()
    # Clean description before matching — raw scraped text contains Facebook
    # UI chrome (nav labels, "TV" in sidebar) that causes false interest matches.
    desc_lower = clean_fb_description(listing.description).lower()
    combined_text = f"{title_lower} {desc_lower}"

    # --- Strategy 1: Origin tag from watchlist sweep ---
    # The tag means this listing came from a watchlist keyword search, but
    # Facebook search returns loosely-related items (e.g. "smart TV" search
    # returns TV stands and remote control cars).  Verify the listing text
    # actually contains the interest keywords before trusting the tag.
    tagged_id = listing.raw_data.get("_watch_item_id")
    if tagged_id and tagged_id in items_by_id:
        item = items_by_id[tagged_id]
        if InterestMatcher._interest_matches(combined_text, item.interest):
            ctx: dict[str, Any] = {
                "watch_item_id": item.id,
                "interest": item.interest,
                "max_price": item.max_price,
                "threshold": item.notification_threshold,
                "effort": getattr(item, "effort", "normal"),
            }
            if item.notes:
                ctx["notes"] = item.notes
            return ctx
        # Tag doesn't match — fall through to keyword matching
        log.debug(
            "Origin tag mismatch",
            title=listing.title[:40],
            interest=item.interest,
        )

    # --- Strategy 2: Keyword matching with synonym expansion ---
    for item in watchlist_items:
        if InterestMatcher._interest_matches(combined_text, item.interest):
            ctx = {
                "watch_item_id": item.id,
                "interest": item.interest,
                "max_price": item.max_price,
                "threshold": item.notification_threshold,
                "effort": getattr(item, "effort", "normal"),
            }
            if item.notes:
                ctx["notes"] = item.notes
            return ctx

    return None
