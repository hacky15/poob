"""WatchlistMatcher - builds scan queries and matches listings to watch items."""

from __future__ import annotations

from agentic_scraper.sites.base import ScanQuery
from agentic_scraper.storage.models import Deal, DealScore, Listing, WatchItem
from agentic_scraper.utils.logging import get_logger

log = get_logger("scanner.watchlist")


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


class WatchlistMatcher:
    """Fast, deterministic matching of listings against user watch items.

    No LLM calls - uses keyword matching and price comparison only.
    """

    def build_queries(self, watch_items: list[WatchItem]) -> list[ScanQuery]:
        """Convert active WatchItems to ScanQuery objects.

        Args:
            watch_items: Active watch items from the database.

        Returns:
            List of ScanQuery objects, one per watch item.
        """
        queries: list[ScanQuery] = []
        for item in watch_items:
            queries.append(ScanQuery(
                keywords=item.keywords,
                max_price=item.max_price,
                location=item.location,
                radius_miles=item.radius_miles,
                category=item.category,
            ))
        return queries

    def match(
        self, listings: list[Listing], watch_items: list[WatchItem]
    ) -> list[Deal]:
        """Match listings against watch items using keyword and price filters.

        For each (listing, watch_item) pair:
        - Check if any keyword from the watch appears in the listing title
        - Check if the listing price is within the watch's max_price
        - Calculate discount percentage and assign a DealScore

        Args:
            listings: New listings to evaluate.
            watch_items: Active watch items to match against.

        Returns:
            List of Deal objects for matching pairs.
        """
        deals: list[Deal] = []

        for listing in listings:
            for watch in watch_items:
                if not self._keywords_match(listing.title, watch.keywords):
                    continue

                if not self._price_within_budget(listing.price, watch.max_price):
                    continue

                discount_pct = self._calculate_discount(listing.price, watch.max_price)
                score = _score_from_discount(discount_pct)

                deal = Deal(
                    listing_id=listing.id or "",
                    watch_item_id=watch.id,
                    score=score,
                    estimated_market_price=watch.max_price,
                    discount_pct=discount_pct,
                )
                deals.append(deal)

        log.info(
            "Watchlist matching complete",
            listings=len(listings),
            watches=len(watch_items),
            deals_found=len(deals),
        )
        return deals

    @staticmethod
    def _keywords_match(title: str, keywords: str) -> bool:
        """Check if keywords appear in the listing title (case-insensitive).

        Uses substring matching - all words in the keywords string must
        appear somewhere in the title.
        """
        title_lower = title.lower()
        keyword_words = keywords.lower().split()
        return all(word in title_lower for word in keyword_words)

    @staticmethod
    def _price_within_budget(price: float | None, max_price: float | None) -> bool:
        """Check if listing price is within the watch's budget.

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
