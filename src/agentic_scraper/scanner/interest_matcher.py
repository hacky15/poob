"""InterestMatcher - matches patrol-discovered listings against user interests.

Two matching strategies:
- **Keyword matching** (`match` / `match_single`): Fast, deterministic, no LLM.
  Used for pre-filtering before any LLM runs. Tolerates false positives
  because the cost of keeping an extra listing is low.
- **LLM-informed matching** (`match_with_identification`): Uses the
  ItemIdentification that SmartDealRadar already produced. Checks whether
  the identification matches the user's interest without an extra LLM call.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentic_scraper.storage.models import Deal, DealScore, Listing, WatchItem
from agentic_scraper.utils.logging import get_logger

if TYPE_CHECKING:
    from agentic_scraper.skills.models import ItemIdentification

log = get_logger("scanner.interest_matcher")

# Bidirectional synonym map for fuzzy interest matching.
# Both the key and its synonyms will match against listing titles.
# Extensible: add new entries as needed.
_SYNONYMS: dict[str, list[str]] = {
    "ps5": ["playstation 5", "playstation5", "play station 5", "gaming console"],
    "playstation 5": ["ps5", "gaming console"],
    "playstation5": ["ps5", "gaming console"],
    "ps4": ["playstation 4", "playstation4", "gaming console"],
    "playstation 4": ["ps4", "gaming console"],
    "xbox": ["xbox series x", "xbox series s", "gaming console"],
    "xbox series x": ["xbox", "gaming console"],
    "xbox series s": ["xbox", "gaming console"],
    "nintendo switch": ["switch oled", "switch lite", "gaming console"],
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


def _word_in_text_fuzzy_plural(word: str, text: str) -> bool:
    """Check if ``word`` appears in ``text``, tolerating plural/singular mismatch.

    Handles common English plural forms:
    - word ends in 's' → also try without 's' (legos → lego)
    - word ends in 'es' → also try without 'es' (dishes → dish)
    - word ends in 'ies' → also try with 'y' (batteries → battery)
    - word does NOT end in 's' → also try with 's' appended (lego → legos)
    """
    if word in text:
        return True
    # Try de-pluralizing
    if word.endswith("ies") and len(word) > 4:
        if word[:-3] + "y" in text:
            return True
    if word.endswith("es") and len(word) > 3:
        if word[:-2] in text:
            return True
    if word.endswith("s") and len(word) > 2:
        if word[:-1] in text:
            return True
    # Try pluralizing
    if not word.endswith("s"):
        if word + "s" in text:
            return True
    return False


class InterestMatcher:
    """Matches listings against user watchlist interests.

    Two matching strategies:

    1. **Keyword matching** (`match` / `match_single`): Fast, no LLM.
       Used for pre-filtering before SmartDealRadar runs. Intentionally
       permissive -- false positives are fine since the cost is just one
       more LLM eval.

    2. **LLM-informed matching** (`match_with_identification`): Uses the
       ItemIdentification from SmartDealRadar to determine whether the
       item actually matches what the user wants. No extra LLM call --
       the identification is already computed.
    """

    def match(
        self, listings: list[Listing], interests: list[WatchItem]
    ) -> list[Deal]:
        """Fast keyword match for pre-filtering.

        Intentionally permissive: allows false positives so that listings
        survive the freshness filter to reach LLM evaluation.
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

        log.debug(
            "Interest matching complete",
            listings=len(listings),
            interests=len(interests),
            deals_found=len(deals),
        )
        return deals

    def match_single(
        self, listing: Listing, interests: list[WatchItem]
    ) -> list[Deal]:
        """Fast keyword match for a single listing — used for pre-filtering only."""
        return self.match([listing], interests)

    def match_with_identification(
        self,
        listing: Listing,
        interests: list[WatchItem],
        identification: ItemIdentification,
    ) -> list[Deal]:
        """LLM-informed matching using SmartDealRadar's item identification.

        Instead of keyword matching on the raw title, this checks the LLM's
        understanding of what the item actually IS against each interest.
        The LLM already identified "Coffee Table Book" as a book in
        category "books/art" — so it won't match interest "coffee table".

        The identification cost is zero — SmartDealRadar already paid for it.

        Args:
            listing: The listing to match.
            interests: Active watchlist items to match against.
            identification: ItemIdentification from SmartDealRadar.

        Returns:
            List of Deal objects for matching interests.
        """
        deals: list[Deal] = []

        # Use the LLM's identified item name + category for matching
        item_name = identification.item_name.lower()
        category = (identification.category or "").lower()

        for interest in interests:
            if not self._identification_matches(
                item_name, category, listing.title.lower(), interest.interest
            ):
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

        log.debug(
            "LLM-informed interest matching",
            item_name=identification.item_name[:40],
            category=category,
            interests=len(interests),
            deals_found=len(deals),
        )
        return deals

    @staticmethod
    def _identification_matches(
        item_name: str,
        category: str,
        title_lower: str,
        interest: str,
    ) -> bool:
        """Check if the LLM-identified item matches a user interest.

        Uses a two-gate approach:
        1. **Category match** (strongest): interest words appear in category → MATCH.
        2. **Item name + category confirmation**: interest words in item_name,
           AND at least one word from the interest/synonym set also appears
           in the category. This prevents "coffee table book" (category
           "books/art") from matching interest "coffee table".

        Args:
            item_name: LLM-identified item name (lowercased).
            category: LLM-identified category (lowercased, hierarchical).
            title_lower: Raw listing title (lowercased, for fallback).
            interest: User's interest string.

        Returns:
            True if the item matches the interest.
        """
        if not interest:
            return False

        interest_lower = interest.lower()
        interest_words = interest_lower.split()
        all_synonyms = _SYNONYMS.get(interest_lower, [])

        # Collect ALL words from interest + synonyms for category confirmation.
        # e.g. "ps5" collects {"ps5", "playstation", "5", "gaming", "console"}
        category_check_words: set[str] = set(interest_words)
        for syn in all_synonyms:
            category_check_words.update(syn.split())

        # --- Strategy 1: Category match (strongest signal) ---
        # If the interest phrase appears in the LLM's category hierarchy,
        # it's definitely a match regardless of item_name.
        if all(word in category for word in interest_words):
            return True
        for syn in all_synonyms:
            if all(word in category for word in syn.split()):
                return True

        # --- Strategy 2: Item name match + category confirmation ---
        # The interest appears in item_name, but we require at least one
        # word from the interest/synonym set to also appear in the category.
        # This prevents cross-category false positives like:
        #   "coffee table book" (books/art) matching "coffee table"
        #   "PS5 Wall Poster" (art/poster) matching "PS5"
        name_match = all(word in item_name for word in interest_words)

        if not name_match:
            for syn in all_synonyms:
                if all(word in item_name for word in syn.split()):
                    name_match = True
                    break

        if not name_match and len(interest_words) > 1:
            for i, word in enumerate(interest_words):
                for synonym in _SYNONYMS.get(word, []):
                    expanded = interest_words[:i] + synonym.split() + interest_words[i + 1:]
                    if all(w in item_name for w in expanded):
                        name_match = True
                        category_check_words.update(expanded)
                        break
                if name_match:
                    break

        if name_match:
            # Category confirmation: at least one relevant word in category
            return any(word in category for word in category_check_words)

        return False

    @staticmethod
    def _interest_matches(title: str, interest: str) -> bool:
        """Fast keyword match on raw listing title.

        Used for pre-filtering before any LLM runs. Intentionally permissive --
        false positives just mean an extra listing reaches LLM evaluation where
        `match_with_identification` makes the real decision.
        """
        if not interest:
            return False

        title_lower = title.lower()
        interest_lower = interest.lower()

        # Stage 1: Exact word matching (fast path)
        interest_words = interest_lower.split()
        if all(word in title_lower for word in interest_words):
            return True

        # Stage 1b: Plural/singular normalization — "legos" matches "lego"
        # and vice versa. Covers the most common English plural forms.
        if all(
            _word_in_text_fuzzy_plural(word, title_lower)
            for word in interest_words
        ):
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
                    expanded = interest_words[:i] + synonym.split() + interest_words[i + 1:]
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
