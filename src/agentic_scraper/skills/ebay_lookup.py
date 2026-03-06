"""EbayLookupTool - eBay sold price lookup via web search.

Searches for eBay sold listings using Tavily API and extracts prices
from the search result snippets.
"""

from __future__ import annotations

import re
import statistics
from typing import TYPE_CHECKING

from agentic_scraper.skills.models import PriceLookupResult
from agentic_scraper.utils.logging import get_logger

if TYPE_CHECKING:
    from agentic_scraper.skills.web_search import SearchProvider

log = get_logger("skills.ebay_lookup")

# Source prefixes that reverse-image-search APIs prepend to product titles.
_SOURCE_PREFIX_RE = re.compile(
    r"^(Amazon\.com:\s*|eBay:\s*|Walmart\.com:\s*|Target:\s*|Best Buy:\s*"
    r"|Etsy:\s*|Wayfair:\s*|Home Depot:\s*|Lowe's:\s*)",
    re.IGNORECASE,
)


def _sanitize_search_query(query: str) -> str:
    """Sanitize a product name for use in Tavily search queries.

    Strips e-commerce source prefixes, special characters that trigger
    Tavily 432 Forbidden errors, trailing ellipses/dots, and truncates
    overly long queries.
    """
    # Strip source prefixes
    query = _SOURCE_PREFIX_RE.sub("", query).strip()
    # Remove trailing ellipses/dots
    query = re.sub(r"[.\s]+$", "", query)
    # Remove characters that cause Tavily 432: quotes, colons, ampersands, etc.
    query = query.replace('"', "").replace("'", "").replace("&", "and")
    query = re.sub(r"[^\w\s.,/$-]", " ", query)
    # Collapse multiple spaces
    query = re.sub(r"\s{2,}", " ", query).strip()
    # Truncate to 80 chars at word boundary
    if len(query) > 80:
        truncated = query[:80].rsplit(" ", 1)[0]
        query = truncated if len(truncated) > 40 else query[:80]
    return query

# Price regex: matches $X, $X.XX, $X,XXX.XX
_PRICE_RE = re.compile(r"\$(\d[\d,]*\.?\d{0,2})")


def extract_prices_from_text(text: str) -> list[float]:
    """Extract dollar prices from search result text.

    Finds all $-prefixed values and filters obvious outliers.

    Args:
        text: Plain text from search results.

    Returns:
        List of extracted price values.
    """
    prices: list[float] = []
    for match in _PRICE_RE.finditer(text):
        try:
            val = float(match.group(1).replace(",", ""))
            if 1.0 <= val <= 50_000.0:
                prices.append(val)
        except ValueError:
            continue

    return prices


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
    """Look up sold prices on eBay via Tavily web search.

    Searches for "eBay sold {item}" and extracts prices from the
    search result text snippets.

    Args:
        search_provider: Tavily web search provider.
    """

    def __init__(self, search_provider: SearchProvider) -> None:
        self._search = search_provider

    async def run(
        self, query: str, condition: str | None = None
    ) -> PriceLookupResult:
        """Search for eBay sold prices and compute price statistics.

        Args:
            query: Search query for the item.
            condition: Optional condition filter.

        Returns:
            PriceLookupResult with market price data.
        """
        # Sanitize query before building search string (defense against
        # Tavily 432 errors from special chars and source prefixes)
        query = _sanitize_search_query(query)
        if not query:
            log.warning("eBay query empty after sanitization")
            return PriceLookupResult(
                sample_count=0, source="ebay_sold",
                search_query="", confidence=0.0,
            )

        # Build search query targeting eBay sold listings.
        # Use "site:ebay.com" in the query string instead of the include_domains
        # API parameter — the API filter may require a paid Tavily plan and causes
        # 432 Forbidden on free tier.
        search_parts = [query]
        if condition and condition not in ("new",):
            search_parts.append(condition)
        search_parts.append("sold price site:ebay.com")
        search_query = " ".join(search_parts)

        try:
            text = await self._search.search(search_query)

            if not text:
                log.warning("eBay search returned no results", query=search_query[:60])
                return PriceLookupResult(
                    sample_count=0, source="ebay_sold",
                    search_query=search_query, confidence=0.0,
                )

            prices = extract_prices_from_text(text)

            log.info(
                "eBay lookup complete",
                query=search_query[:60],
                prices_found=len(prices),
            )

            return compute_price_stats(
                prices, query=search_query, source="ebay_sold"
            )

        except Exception as exc:
            log.warning(
                "eBay lookup failed",
                error=str(exc),
                query=search_query[:60],
            )
            return PriceLookupResult(
                sample_count=0, source="ebay_sold",
                search_query=search_query, confidence=0.0,
            )
