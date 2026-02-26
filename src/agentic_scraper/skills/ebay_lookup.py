"""EbayLookupTool - eBay sold price lookup via HTTP scraping."""

from __future__ import annotations

import re
import statistics
from urllib.parse import quote_plus

import httpx

from agentic_scraper.skills.models import PriceLookupResult
from agentic_scraper.utils.logging import get_logger

log = get_logger("skills.ebay_lookup")

# eBay sold items search URL pattern
EBAY_SOLD_URL = (
    "https://www.ebay.com/sch/i.html?_nkw={query}&LH_Complete=1&LH_Sold=1&_sop=13"
)

# Browser-like headers to avoid basic bot detection
EBAY_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def parse_ebay_sold_html(html: str) -> list[dict]:
    """Parse eBay search results HTML to extract item titles and prices.

    Args:
        html: Raw HTML from eBay sold items search.

    Returns:
        List of dicts with 'title' and 'price' keys.
    """
    items: list[dict] = []

    # Find all s-item blocks
    item_pattern = re.compile(
        r'class="s-item__title[^"]*"[^>]*>(.*?)</span>.*?'
        r'class="s-item__price"[^>]*>(.*?)</span>',
        re.DOTALL,
    )

    for match in item_pattern.finditer(html):
        title_raw = match.group(1).strip()
        price_raw = match.group(2).strip()

        # Clean HTML tags from title
        title = re.sub(r"<[^>]+>", "", title_raw).strip()

        # Skip placeholder items
        if title.lower() in ("shop on ebay", ""):
            continue

        # Extract first price (handles "$100.00 to $200.00" ranges)
        price_match = re.search(r"\$([0-9,]+\.?\d*)", price_raw)
        if not price_match:
            continue

        try:
            price = float(price_match.group(1).replace(",", ""))
        except ValueError:
            continue

        items.append({"title": title, "price": price})

    return items


def compute_price_stats(
    prices: list[float], query: str, source: str
) -> PriceLookupResult:
    """Compute price statistics from a list of prices.

    Args:
        prices: List of price values.
        query: The search query used.
        source: Data source identifier.

    Returns:
        PriceLookupResult with computed statistics.
    """
    if not prices:
        return PriceLookupResult(
            sample_count=0,
            source=source,
            search_query=query,
            confidence=0.0,
        )

    sorted_prices = sorted(prices)
    n = len(sorted_prices)

    median = statistics.median(sorted_prices)
    average = statistics.mean(sorted_prices)

    # Confidence scales with sample count, capped at 0.95
    if n >= 10:
        confidence = 0.9
    elif n >= 5:
        confidence = 0.75
    elif n >= 3:
        confidence = 0.6
    elif n >= 1:
        confidence = 0.3
    else:
        confidence = 0.0

    # Lower confidence if prices are very spread out (high variance)
    if n >= 2:
        stdev = statistics.stdev(sorted_prices)
        cv = stdev / average if average > 0 else 0
        if cv > 0.5:  # Coefficient of variation > 50%
            confidence *= 0.7

    return PriceLookupResult(
        median_price=round(median, 2),
        average_price=round(average, 2),
        min_price=round(sorted_prices[0], 2),
        max_price=round(sorted_prices[-1], 2),
        sample_count=n,
        source=source,
        search_query=query,
        confidence=round(min(confidence, 0.95), 2),
    )


class EbayLookupTool:
    """Look up sold prices on eBay via HTTP scraping.

    Args:
        timeout_seconds: HTTP request timeout.
    """

    def __init__(self, timeout_seconds: int = 10) -> None:
        self._timeout = timeout_seconds

    async def run(
        self, query: str, condition: str | None = None
    ) -> PriceLookupResult:
        """Search eBay sold listings and compute price statistics.

        Args:
            query: Search query for the item.
            condition: Optional condition filter.

        Returns:
            PriceLookupResult with market price data.
        """
        search_query = query
        if condition and condition not in ("new",):
            search_query = f"{query} {condition}"

        url = EBAY_SOLD_URL.format(query=quote_plus(search_query))

        try:
            async with httpx.AsyncClient(
                headers=EBAY_HEADERS,
                timeout=self._timeout,
                follow_redirects=True,
            ) as client:
                response = await client.get(url)

            if response.status_code != 200:
                log.warning(
                    "eBay HTTP request failed",
                    status=response.status_code,
                    query=search_query,
                )
                return PriceLookupResult(
                    sample_count=0, source="ebay_sold",
                    search_query=search_query, confidence=0.0,
                )

            items = parse_ebay_sold_html(response.text)
            prices = [item["price"] for item in items]

            log.info(
                "eBay lookup complete",
                query=search_query,
                items_found=len(items),
            )

            return compute_price_stats(prices, query=search_query, source="ebay_sold")

        except Exception as exc:
            log.warning(
                "eBay lookup failed",
                error=str(exc),
                query=search_query,
            )
            return PriceLookupResult(
                sample_count=0, source="ebay_sold",
                search_query=search_query, confidence=0.0,
            )
