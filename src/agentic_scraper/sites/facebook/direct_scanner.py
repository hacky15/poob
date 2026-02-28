"""Direct CDP-based scanner for Facebook Marketplace.

Navigates directly using Page.goto() and extracts listing data
via JavaScript DOM queries, falling back to LLM extraction only
when the DOM structure is unrecognized. Eliminates the need for
an LLM-powered browser-use Agent for navigation.
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING

from agentic_scraper.browser.page_actions import navigate_and_wait
from agentic_scraper.browser.stealth import apply_scroll_pattern
from agentic_scraper.sites.base import ScanQuery, ScanResult
from agentic_scraper.sites.facebook.categories import infer_category, is_relevant_to_category
from agentic_scraper.sites.facebook.extraction_prompts import EXTRACT_LISTINGS_PROMPT
from agentic_scraper.sites.facebook.js_extractor import EXTRACT_LISTINGS_JS
from agentic_scraper.sites.facebook.parser import parse_listings
from agentic_scraper.utils.logging import get_logger

if TYPE_CHECKING:
    from typing import Any

log = get_logger("sites.facebook.direct_scanner")

# Minimum JS results before triggering LLM fallback
_MIN_JS_RESULTS = 3


class DirectScanner:
    """Scans Facebook Marketplace using direct CDP page control.

    Navigates to search URLs, scrolls to load listings, and extracts
    data via JavaScript DOM queries. Falls back to LLM-based extraction
    when JS returns insufficient results (e.g., Facebook changed their DOM).

    Args:
        max_listings: Maximum listings to extract per scan.
        scroll_steps: Number of scroll steps to load more listings.
    """

    def __init__(
        self,
        *,
        max_listings: int = 20,
        scroll_steps: int = 5,
    ) -> None:
        self._max_listings = max_listings
        self._scroll_steps = scroll_steps

    def _build_search_url(self, query: ScanQuery) -> str:
        """Build Facebook Marketplace search URL from query parameters.

        When keywords are provided, uses /marketplace/search/ for keyword search.
        When keywords are empty (browse mode), uses /marketplace/ to browse
        the latest local listings without any search filter.

        Category filtering happens post-scrape via is_relevant_to_category()
        so we don't miss listings from sellers who skip categorization.

        Args:
            query: Search parameters.

        Returns:
            Fully formed search URL with query, sort, and price filter.
        """
        if not query.keywords.strip():
            # Browse mode: latest local listings, no keyword filter
            url = "https://www.facebook.com/marketplace/?sortBy=creation_time_descend"
        else:
            url = (
                f"https://www.facebook.com/marketplace/search/"
                f"?query={query.keywords.replace(' ', '+')}"
                f"&sortBy=creation_time_descend"
            )
        if query.max_price is not None:
            url += f"&maxPrice={query.max_price:.0f}"
        return url

    async def scan(
        self,
        query: ScanQuery,
        browser_manager: object,
        llm: Any | None = None,
    ) -> ScanResult:
        """Execute a direct scan of Facebook Marketplace.

        Pipeline:
        1. Get Page from browser_manager
        2. Navigate to search URL
        3. Scroll to load more listings
        4. Extract via JavaScript (0 LLM calls)
        5. If JS returns < _MIN_JS_RESULTS, fall back to LLM extraction
        6. Parse results through existing parser
        7. Return ScanResult

        Args:
            query: Search parameters.
            browser_manager: BrowserManager with get_page() method.
            llm: Optional LLM for extraction fallback. If None, no fallback.

        Returns:
            ScanResult with parsed listings and any errors.
        """
        start_time = time.monotonic()
        errors: list[str] = []

        try:
            page = await browser_manager.get_page()
        except RuntimeError as exc:
            return ScanResult(
                errors=[str(exc)],
                scan_duration_seconds=time.monotonic() - start_time,
            )

        search_url = self._build_search_url(query)

        try:
            # Navigate to search page
            await navigate_and_wait(page, search_url)
            log.info("Navigated to search page", url=search_url)

            # Scroll to load more listings
            await apply_scroll_pattern(page, steps=self._scroll_steps)

            # Try JavaScript DOM extraction first
            raw_data = await self._extract_via_js(page)

            # Fall back to LLM if JS didn't return enough
            if len(raw_data) < _MIN_JS_RESULTS:
                log.info(
                    "JS extraction insufficient, trying LLM fallback",
                    js_results=len(raw_data),
                    min_required=_MIN_JS_RESULTS,
                )
                llm_data = await self._extract_via_llm(page, llm)
                if llm_data:
                    raw_data = llm_data

            # Convert to JSON string for the parser
            raw_json = json.dumps(raw_data) if raw_data else "[]"
            listings = parse_listings(raw_json, site="facebook_marketplace")

            # Filter out irrelevant listings based on category.
            # Use explicit category if set, otherwise infer from keywords.
            category = query.category or infer_category(query.keywords)
            if category:
                before = len(listings)
                listings = [
                    l for l in listings
                    if is_relevant_to_category(l.title, category)
                ]
                filtered = before - len(listings)
                if filtered > 0:
                    log.info(
                        "Category relevance filter applied",
                        category=category,
                        inferred=query.category is None,
                        before=before,
                        after=len(listings),
                        filtered_out=filtered,
                    )

            # Trim to max_listings
            listings = listings[: self._max_listings]

        except Exception as exc:
            log.error("Direct scan failed", error=str(exc), keywords=query.keywords)
            errors.append(f"Direct scan failed: {exc}")
            listings = []

        duration = time.monotonic() - start_time
        log.info(
            "Direct scan complete",
            keywords=query.keywords,
            listings_found=len(listings),
            duration=f"{duration:.1f}s",
        )

        return ScanResult(
            listings=listings,
            errors=errors,
            scan_duration_seconds=duration,
        )

    async def _extract_via_js(self, page: object) -> list[dict]:
        """Extract listings from DOM using JavaScript.

        Args:
            page: CDP Page instance.

        Returns:
            List of listing dicts extracted from the DOM.
        """
        try:
            result = await page.evaluate(EXTRACT_LISTINGS_JS)

            # page.evaluate() returns the value directly — could be list or str
            if isinstance(result, list):
                return result
            if isinstance(result, str) and result:
                parsed = json.loads(result)
                return parsed if isinstance(parsed, list) else []
            return []

        except Exception as exc:
            log.warning("JS extraction failed", error=str(exc))
            return []

    async def _extract_via_llm(
        self, page: object, llm: Any | None
    ) -> list[dict]:
        """Fallback: extract page markdown and use LLM for JSON extraction.

        Args:
            page: CDP Page instance.
            llm: LangChain-compatible chat model. If None, returns empty.

        Returns:
            List of listing dicts extracted by the LLM.
        """
        if llm is None:
            log.info("No LLM provided for fallback extraction")
            return []

        try:
            content, _stats = await page._extract_clean_markdown()
            if not content or not content.strip():
                return []

            prompt = EXTRACT_LISTINGS_PROMPT.format(page_content=content)
            response = await llm.ainvoke(prompt)
            raw_text = response.content if hasattr(response, "content") else str(response)

            # Parse the LLM response
            data = json.loads(raw_text)
            if isinstance(data, dict) and "listings" in data:
                return data["listings"]
            if isinstance(data, list):
                return data
            return []

        except Exception as exc:
            log.warning("LLM extraction fallback failed", error=str(exc))
            return []
