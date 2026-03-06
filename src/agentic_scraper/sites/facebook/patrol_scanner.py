"""Patrol-based scanner for Facebook Marketplace.

Sweeps category pages by browsing ALL new listings (not keyword search),
using URL construction with cache-busting radius oscillation.
"""

from __future__ import annotations

import json

from agentic_scraper.browser.page_actions import navigate_and_wait
from agentic_scraper.browser.stealth import apply_scroll_pattern
from agentic_scraper.sites.facebook.categories import CATEGORY_SLUG_MAP
from agentic_scraper.sites.facebook.js_extractor import EXTRACT_LISTINGS_JS
from agentic_scraper.sites.facebook.parser import parse_listings
from agentic_scraper.storage.models import Listing
from agentic_scraper.utils.logging import get_logger

log = get_logger("sites.facebook.patrol_scanner")


def build_patrol_url(
    category: str | None,
    *,
    radius: int = 20,
    days_since_listed: int = 1,
    exact: bool = False,
) -> str:
    """Build a Facebook Marketplace patrol URL for a category or all listings.

    Args:
        category: Application category name (e.g. "electronics"), or None for all.
        radius: Search radius in miles (oscillated for cache busting).
        days_since_listed: Filter to listings posted within this many days.
        exact: Whether to use exact=true parameter.

    Returns:
        Fully formed Facebook Marketplace URL for category browsing.
    """
    base = "https://www.facebook.com/marketplace"

    if category:
        slug = CATEGORY_SLUG_MAP.get(category.lower().strip(), category)
        path = f"/category/{slug}"
    else:
        path = ""

    params = [
        "sortBy=creation_time_descend",
        f"daysSinceListed={days_since_listed}",
        "deliveryMethod=local_pick_up",
    ]
    if not exact:
        params.append("exact=false")
    if radius:
        params.append(f"radius={radius}")

    return f"{base}{path}?{'&'.join(params)}"


class RadiusOscillator:
    """Cycles through radius values to bust Facebook's caching.

    Facebook serves cached results when the same URL is requested repeatedly.
    By varying the radius parameter, we force the backend to recalculate
    the geographic bounding box and serve fresh data.
    """

    def __init__(self, base_radius: int = 20, jitter: int = 4) -> None:
        self._radii = [
            base_radius,
            base_radius + jitter // 2,
            base_radius - jitter // 2,
            base_radius + jitter,
        ]
        self._index = 0

    def next(self) -> int:
        """Return the next radius value in the cycle."""
        r = self._radii[self._index]
        self._index = (self._index + 1) % len(self._radii)
        return r


class PatrolScanner:
    """Sweeps Facebook Marketplace category pages to collect surface-level listing data.

    Does not do deep inspection - returns basic listings (title, price, URL, thumbnail).
    Uses JS extraction from the DOM, same as DirectScanner.

    Args:
        radius_oscillator: RadiusOscillator instance for cache busting.
        scroll_steps: Number of scroll steps per page to load more listings.
        days_since_listed: Filter to listings posted within this many days.
        fixed_radius: If set, always use this radius (disables oscillation).
    """

    def __init__(
        self,
        *,
        radius_oscillator: RadiusOscillator | None = None,
        scroll_steps: int = 5,
        days_since_listed: int = 1,
        fixed_radius: int | None = None,
    ) -> None:
        self._oscillator = radius_oscillator or RadiusOscillator()
        self._scroll_steps = scroll_steps
        self._days_since_listed = days_since_listed
        self._fixed_radius = fixed_radius

    async def sweep_category(
        self,
        page: object,
        category: str | None,
        *,
        days_since_listed: int | None = None,
    ) -> list[Listing]:
        """Navigate to a category URL, scroll, and extract surface listings.

        Args:
            page: A browser-use CDP Page instance.
            category: Category to sweep, or None for all listings.
            days_since_listed: Override days filter.

        Returns:
            List of Listing objects with surface data (no full description).
        """
        radius = self._fixed_radius if self._fixed_radius else self._oscillator.next()
        days = days_since_listed or self._days_since_listed
        url = build_patrol_url(category, radius=radius, days_since_listed=days)

        try:
            await navigate_and_wait(page, url)
            await apply_scroll_pattern(page, steps=self._scroll_steps)
            raw_data = await page.evaluate(EXTRACT_LISTINGS_JS)

            if isinstance(raw_data, str):
                raw_data = json.loads(raw_data)
            if not isinstance(raw_data, list):
                raw_data = []

            raw_json = json.dumps(raw_data)
            listings = parse_listings(raw_json, site="facebook_marketplace")

            log.info(
                "Category sweep complete",
                category=category or "all",
                radius=radius,
                listings_found=len(listings),
            )
            return listings

        except Exception as exc:
            log.error("Category sweep failed", category=category, error=str(exc))
            return []

    async def sweep_search(
        self,
        page: object,
        keywords: str,
        *,
        max_price: float | None = None,
        min_price: float | None = None,
        location_slug: str | None = None,
        condition: str | None = None,
        radius_miles: int | None = None,
    ) -> list[Listing]:
        """Search Facebook Marketplace by keywords and extract listings.

        Used by the patrol engine to search for specific watchlist items,
        complementing the main category sweep.

        Args:
            page: A browser-use CDP Page instance.
            keywords: Search keywords (e.g. "PS5", "dining table").
            max_price: Optional maximum price filter.
            min_price: Optional minimum price filter.
            location_slug: FB city slug (e.g. "madison", "green-bay") for URL path.
            condition: FB condition filter (e.g. "used_good", "used_like_new,used_good").
            radius_miles: Override radius for this search (uses oscillator default if None).

        Returns:
            List of Listing objects from the search results.
        """
        if radius_miles is not None:
            radius = radius_miles
        elif self._fixed_radius:
            radius = self._fixed_radius
        else:
            radius = self._oscillator.next()

        # Build URL path — location slug goes before /search/
        if location_slug:
            search_path = f"/marketplace/{location_slug}/search/"
        else:
            search_path = "/marketplace/search/"

        url = (
            f"https://www.facebook.com{search_path}"
            f"?query={keywords.replace(' ', '+')}"
            f"&sortBy=creation_time_descend"
            f"&daysSinceListed={self._days_since_listed}"
            f"&deliveryMethod=local_pick_up"
            f"&radius={radius}"
        )
        if max_price is not None:
            url += f"&maxPrice={max_price:.0f}"
        if min_price is not None:
            url += f"&minPrice={min_price:.0f}"
        if condition:
            url += f"&itemCondition={condition}"

        try:
            await navigate_and_wait(page, url)
            await apply_scroll_pattern(page, steps=self._scroll_steps)
            raw_data = await page.evaluate(EXTRACT_LISTINGS_JS)

            if isinstance(raw_data, str):
                raw_data = json.loads(raw_data)
            if not isinstance(raw_data, list):
                raw_data = []

            raw_json = json.dumps(raw_data)
            listings = parse_listings(raw_json, site="facebook_marketplace")

            log.info(
                "Search sweep complete",
                keywords=keywords,
                radius=radius,
                max_price=max_price,
                min_price=min_price,
                location=location_slug,
                condition=condition,
                listings_found=len(listings),
            )
            return listings

        except Exception as exc:
            log.error("Search sweep failed", keywords=keywords, error=str(exc))
            return []
