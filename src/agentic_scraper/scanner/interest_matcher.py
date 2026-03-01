"""InterestMatcher - matches patrol-discovered listings against user interests."""

from __future__ import annotations

from agentic_scraper.storage.models import Deal, DealScore, Listing, WatchItem
from agentic_scraper.utils.logging import get_logger

log = get_logger("scanner.interest_matcher")

# Bidirectional synonym map for fuzzy interest matching.
# Both the key and its synonyms will match against listing titles.
# Extensible: add new entries as needed.
_SYNONYMS: dict[str, list[str]] = {
    "ps5": ["playstation 5", "playstation5", "play station 5"],
    "playstation 5": ["ps5"],
    "playstation5": ["ps5"],
    "ps4": ["playstation 4", "playstation4"],
    "playstation 4": ["ps4"],
    "xbox": ["xbox series x", "xbox series s"],
    "xbox series x": ["xbox"],
    "xbox series s": ["xbox"],
    "nintendo switch": ["switch oled", "switch lite"],
    "fridge": ["refrigerator"],
    "refrigerator": ["fridge"],
    "tv": ["television", "smart tv"],
    "television": ["tv", "smart tv"],
    "couch": ["sofa", "loveseat", "sectional"],
    "sofa": ["couch", "loveseat"],
    "sectional": ["couch", "sofa"],
    "washer": ["washing machine"],
    "washing machine": ["washer"],
    "dryer": ["clothes dryer"],
    "clothes dryer": ["dryer"],
    "laptop": ["notebook", "chromebook"],
    "notebook": ["laptop"],
    "chromebook": ["laptop"],
    "ipad": ["tablet", "ipad pro", "ipad air"],
    "tablet": ["ipad"],
    "airpods": ["earbuds", "airpods pro"],
    "earbuds": ["airpods"],
    "gpu": ["graphics card", "video card"],
    "graphics card": ["gpu", "video card"],
    "video card": ["gpu", "graphics card"],
    "monitor": ["display"],
    "display": ["monitor"],
    "bike": ["bicycle"],
    "bicycle": ["bike"],
    "desk": ["standing desk", "computer desk"],
    "dresser": ["chest of drawers"],
    "lawnmower": ["lawn mower", "mower"],
    "lawn mower": ["lawnmower", "mower"],
}


def _score_from_discount(discount_pct: float) -> DealScore:
    """Assign a DealScore based on the discount percentage.

    Args:
        discount_pct: Discount as a percentage (0-100).

    Returns:
        DealScore based on thresholds.
    """
    if discount_pct >= 60:
        return DealScore.INCREDIBLE
    if discount_pct >= 40:
        return DealScore.GREAT
    if discount_pct >= 20:
        return DealScore.GOOD
    if discount_pct > 0:
        return DealScore.FAIR
    return DealScore.UNKNOWN


class InterestMatcher:
    """Fast, deterministic matching of listings against user interests.

    No LLM calls - uses keyword matching and price comparison only.
    Interests are things users want to look out for (not search queries).
    """

    def match(
        self, listings: list[Listing], interests: list[WatchItem]
    ) -> list[Deal]:
        """Match listings against interests using keyword and price filters.

        For each (listing, interest) pair:
        - Check if the interest terms appear in the listing title
        - Check if the listing price is within the interest's max_price
        - Calculate discount percentage and assign a DealScore

        Args:
            listings: New listings to evaluate.
            interests: Active watch items (interests) to match against.

        Returns:
            List of Deal objects for matching pairs.
        """
        deals: list[Deal] = []

        for listing in listings:
            for interest in interests:
                if not self._interest_matches(listing.title, interest.interest):
                    continue

                if not self._price_within_budget(listing.price, interest.max_price):
                    continue

                discount_pct = self._calculate_discount(listing.price, interest.max_price)
                score = _score_from_discount(discount_pct)

                deal = Deal(
                    listing_id=listing.id or "",
                    watch_item_id=interest.id,
                    score=score,
                    estimated_market_price=interest.max_price,
                    discount_pct=discount_pct,
                    llm_reasoning=f"Matches your interest: {interest.interest}",
                )
                deals.append(deal)

        log.info(
            "Interest matching complete",
            listings=len(listings),
            interests=len(interests),
            deals_found=len(deals),
        )
        return deals

    def match_single(
        self, listing: Listing, interests: list[WatchItem]
    ) -> list[Deal]:
        """Match a single listing against all interests.

        Convenience method for the patrol pipeline where listings are
        processed one at a time.

        Args:
            listing: Single listing to evaluate.
            interests: Active watch items (interests) to match against.

        Returns:
            List of Deal objects for matching interests.
        """
        return self.match([listing], interests)

    @staticmethod
    def _interest_matches(title: str, interest: str) -> bool:
        """Check if interest terms appear in the listing title (case-insensitive).

        Uses a three-stage matching strategy:
        1. Exact: all words in the interest must appear in the title
        2. Whole-phrase synonym: try each synonym for the full interest phrase
        3. Per-word synonym: try synonyms for each word in the interest

        This allows "PS5" to match "PlayStation 5 Disc Edition" and
        "fridge" to match "Samsung Refrigerator".
        """
        if not interest:
            return False

        title_lower = title.lower()
        interest_lower = interest.lower()

        # Stage 1: Exact word matching (fast path)
        interest_words = interest_lower.split()
        if all(word in title_lower for word in interest_words):
            return True

        # Stage 2: Whole-phrase synonym lookup
        for synonym_phrase in _SYNONYMS.get(interest_lower, []):
            synonym_words = synonym_phrase.split()
            if all(word in title_lower for word in synonym_words):
                return True

        # Stage 3: Per-word synonym expansion for multi-word interests
        if len(interest_words) > 1:
            for i, word in enumerate(interest_words):
                for synonym in _SYNONYMS.get(word, []):
                    expanded = interest_words[:i] + synonym.split() + interest_words[i + 1 :]
                    if all(w in title_lower for w in expanded):
                        return True

        return False

    @staticmethod
    def _price_within_budget(price: float | None, max_price: float | None) -> bool:
        """Check if listing price is within the interest's budget.

        Returns True if: no max_price set, or no listing price, or price <= max_price.
        """
        if max_price is None:
            return True
        if price is None:
            return True
        return price <= max_price

    @staticmethod
    def _calculate_discount(price: float | None, max_price: float | None) -> float:
        """Calculate discount percentage relative to max_price (used as market proxy).

        Returns 0.0 if either value is missing.
        """
        if price is None or max_price is None or max_price == 0:
            return 0.0
        return ((max_price - price) / max_price) * 100.0
