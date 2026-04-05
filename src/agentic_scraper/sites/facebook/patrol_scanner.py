"""Patrol-based scanner for Facebook Marketplace.

Sweeps category pages by browsing ALL new listings (not keyword search),
using URL construction with cache-busting radius oscillation.
"""

from __future__ import annotations

import json
import re

from agentic_scraper.browser.page_actions import navigate_and_wait
from agentic_scraper.browser.stealth import apply_scroll_pattern, scroll_until_stable
from agentic_scraper.sites.facebook.categories import CATEGORY_SLUG_MAP
from agentic_scraper.sites.facebook.js_extractor import EXTRACT_LISTINGS_JS
from agentic_scraper.sites.facebook.parser import parse_listings
from agentic_scraper.storage.models import Listing
from agentic_scraper.utils.logging import get_logger

log = get_logger("sites.facebook.patrol_scanner")

# ---------------------------------------------------------------------------
# Post-fetch relevance filter
# ---------------------------------------------------------------------------
# Facebook Marketplace with sortBy=creation_time_descend returns loosely
# related items to fill the page (e.g. fireplaces for "smart tv", soap
# holders for "kitchenaid"). This filter drops results that don't contain
# any meaningful query keywords in their title.
#
# Strategy:
# - Tokenize the search query into words (ignore stopwords).
# - A listing passes if its title contains at least one non-stopword query
#   word OR a known synonym/abbreviation.
# - Multi-word brand names (e.g. "kitchen aid" / "kitchenaid") get special
#   handling: compound forms match too.
# ---------------------------------------------------------------------------

_RELEVANCE_STOPWORDS = frozenset({
    "a", "an", "the", "and", "or", "of", "for", "in", "on", "at", "to",
    "with", "by", "from", "is", "it", "my", "new", "used", "good", "set",
    "lot", "over", "the", "this", "that",
})

# Domain-adjacent terms: when a query keyword appears in this map,
# we ALSO accept listings that contain any of the related terms.
# This prevents the filter from being too literal — "espresso machine"
# should let through "coffee maker" and "cappuccino brewer" for triage
# to decide, but should still drop "Russian Stop Watch" and "Fossil watch".
_RELATED_TERMS: dict[str, list[str]] = {
    # Coffee/espresso domain
    "espresso": ["coffee", "latte", "cappuccino", "nespresso", "barista", "moka", "brew"],
    "coffee": ["espresso", "latte", "cappuccino", "nespresso", "keurig", "barista", "brew"],
    "cappuccino": ["espresso", "coffee", "latte", "nespresso", "barista"],
    "latte": ["espresso", "coffee", "cappuccino"],
    # Appliance types
    "machine": ["maker", "brewer", "press", "system"],
    "maker": ["machine", "brewer", "press", "system"],
    # TV domain
    "tv": ["television", "hdtv", "oled", "qled", "roku", "smart tv", "lcd", "led"],
    "television": ["tv", "hdtv", "oled", "qled", "roku"],
    "smart": ["roku", "fire stick", "chromecast", "android tv"],
    # Cookware domain
    "cookware": [
        "pots", "pans", "skillet", "saucepan", "stockpot", "dutch oven", "wok",
        "pot", "pan", "cooker", "roaster",
    ],
    "stainless": ["steel", "clad", "tri-ply"],
    # Gaming / collectibles
    "pokemon": [
        "pokémon", "pikachu", "charizard", "mewtwo", "eevee", "snorlax",
        "soul silver", "heartgold", "reshiram", "zekrom",
    ],
    "cards": ["card", "trading card", "tcg", "booster", "singles"],
    # Toys / building
    "legos": ["lego", "duplo", "bionicle", "minifig", "minifigure", "building blocks", "ninjago"],
    "lego": ["legos", "duplo", "bionicle", "minifig", "minifigure", "building blocks", "ninjago"],
    # Glassware
    "glassware": [
        "glass", "crystal", "stemware", "wine glass", "goblet", "tumbler",
        "champagne", "martini", "cocktail", "drinking glass", "pitcher",
        "decanter", "carafe", "vase",
    ],
    # Baking / cookies
    "cookies": [
        "cookie", "biscuit", "baking", "cookie jar", "cookie sheet",
        "cookie cutter", "baking sheet", "sugar cookie",
    ],
    "cookie": ["cookies", "biscuit", "baking", "cookie jar", "cookie cutter"],
    # Furniture / bathroom
    "toilet": ["bathroom", "restroom", "commode"],
    "shelves": ["shelf", "shelving", "rack", "organizer", "unit"],
    "shelving": ["shelf", "shelves", "rack", "organizer", "unit"],
    "storage": ["organizer", "cabinet", "rack", "bin", "basket"],
    # Outdoor / hiking
    "hiking": ["hike", "trail", "trekking", "camping", "osprey", "kelty", "rei", "daypack"],
    "backpack": [
        "pack", "rucksack", "daypack", "knapsack", "bag", "carry", "osprey",
        "kelty", "deuter", "gregory", "rei",
    ],
    # Kitchen brands
    "kitchenaid": ["kitchen aid", "stand mixer", "artisan"],
}

# Common misspellings → canonical form for _RELATED_TERMS lookup.
# When a keyword doesn't match any key in _RELATED_TERMS, we check
# this map to see if it's a known typo.
_TYPO_CORRECTIONS: dict[str, str] = {
    "cookeware": "cookware",
    "cookwear": "cookware",
    "cookwar": "cookware",
    "expresso": "espresso",
    "televison": "television",
    "telivision": "television",
    "shelfs": "shelves",
    "pokémon": "pokemon",
    "legoes": "legos",
    "glasswear": "glassware",
    "glasswares": "glassware",
    "coookies": "cookies",
    "kichenaid": "kitchenaid",
    "kitchenaide": "kitchenaid",
}


def _tokenize_query(query: str) -> list[str]:
    """Split query into meaningful words, stripping stopwords."""
    words = re.findall(r"[a-z0-9]+", query.lower())
    return [w for w in words if w not in _RELEVANCE_STOPWORDS]


def _expand_keywords(keywords: list[str]) -> set[str]:
    """Expand keywords with domain-adjacent related terms.

    Returns a set of ALL terms that count as a match: the original keywords
    plus any related terms from the _RELATED_TERMS map. Also checks typo
    corrections so "cookeware" activates the "cookware" expansion.
    """
    expanded: set[str] = set(keywords)
    for kw in keywords:
        # Direct lookup first
        related = _RELATED_TERMS.get(kw)
        if not related:
            # Try typo correction: "cookeware" → "cookware"
            corrected = _TYPO_CORRECTIONS.get(kw)
            if corrected:
                related = _RELATED_TERMS.get(corrected)
                expanded.add(corrected)  # Also match the correct spelling
        if related:
            for term in related:
                expanded.add(term)
    return expanded


def filter_by_relevance(
    listings: list[Listing],
    query: str,
    *,
    min_keyword_matches: int = 1,
) -> list[Listing]:
    """Drop listings whose titles have zero overlap with the search query.

    This is a LIGHT sanity check, not a strict relevance engine. Its job is
    to catch OBVIOUSLY wrong results (watches for "espresso machine",
    fireplaces for "smart tv") while letting domain-adjacent items through
    to the triage LLM where the real relevance decision happens.

    Uses related-term expansion so "espresso machine" also accepts "coffee
    maker", "cappuccino brewer", etc.

    Args:
        listings: Raw listings from Facebook search results.
        query: The search keywords used (e.g. "espresso machine").
        min_keyword_matches: Minimum number of query words that must appear
            in the title. Default 1 — at least one meaningful word must match.

    Returns:
        Filtered list of listings with irrelevant results removed.
    """
    keywords = _tokenize_query(query)
    if not keywords:
        return listings  # Can't filter without keywords

    # Expand with domain-adjacent terms
    all_terms = _expand_keywords(keywords)

    # Build compound forms: "kitchen aid" → also match "kitchenaid"
    compounds: list[str] = []
    if len(keywords) >= 2:
        compounds.append("".join(keywords))  # "kitchen" + "aid" → "kitchenaid"

    kept: list[Listing] = []
    dropped = 0

    for listing in listings:
        title_lower = listing.title.lower()

        # Check if ANY expanded term appears in the title
        matches = sum(1 for term in all_terms if term in title_lower)

        # Also check compound form
        if matches < min_keyword_matches and compounds:
            if any(c in title_lower for c in compounds):
                matches = min_keyword_matches

        if matches >= min_keyword_matches:
            kept.append(listing)
        else:
            dropped += 1

    if dropped:
        log.info(
            "Relevance filter applied",
            query=query,
            keywords=keywords,
            expanded_terms=len(all_terms),
            before=len(listings),
            kept=len(kept),
            dropped=dropped,
        )

    return kept


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
        scroll_steps: int = 20,
        search_scroll_steps: int = 30,
        days_since_listed: int = 1,
        fixed_radius: int | None = None,
        scroll_until_stable: bool = True,
        scroll_max_stable_checks: int = 3,
    ) -> None:
        self._oscillator = radius_oscillator or RadiusOscillator()
        self._scroll_steps = scroll_steps
        self._search_scroll_steps = search_scroll_steps
        self._days_since_listed = days_since_listed
        self._fixed_radius = fixed_radius
        self._scroll_until_stable = scroll_until_stable
        self._scroll_max_stable_checks = scroll_max_stable_checks

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

            # Scroll to load listings — prefer scroll-until-stable for deep loading
            if self._scroll_until_stable:
                scrolls = await scroll_until_stable(
                    page,
                    max_scrolls=self._scroll_steps,
                    stable_checks=self._scroll_max_stable_checks,
                )
                log.debug("Scroll-until-stable done", scrolls=scrolls, mode="category")
            else:
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
            f"&exact=true"
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

            # Keyword searches need aggressive scrolling to get past sponsored
            # listings and load real matches. Use scroll-until-stable to exhaust
            # the infinite scroll, or fall back to a high fixed step count.
            if self._scroll_until_stable:
                scrolls = await scroll_until_stable(
                    page,
                    max_scrolls=self._search_scroll_steps,
                    stable_checks=self._scroll_max_stable_checks,
                )
                log.debug(
                    "Scroll-until-stable done",
                    scrolls=scrolls,
                    mode="search",
                    keywords=keywords[:30],
                )
            else:
                await apply_scroll_pattern(page, steps=self._search_scroll_steps)

            raw_data = await page.evaluate(EXTRACT_LISTINGS_JS)

            if isinstance(raw_data, str):
                raw_data = json.loads(raw_data)
            if not isinstance(raw_data, list):
                raw_data = []

            raw_json = json.dumps(raw_data)
            listings = parse_listings(raw_json, site="facebook_marketplace")

            # Post-fetch relevance filter: Facebook with creation_time sort
            # returns loosely related garbage. Kill it before it wastes LLM calls.
            # For multi-word queries (e.g. "pokemon cards"), require 2 keyword
            # matches so single-word coincidences ("Index Card Holder") are dropped.
            raw_count = len(listings)
            keyword_tokens = _tokenize_query(keywords)
            min_matches = 2 if len(keyword_tokens) >= 2 else 1
            listings = filter_by_relevance(listings, keywords, min_keyword_matches=min_matches)

            log.info(
                "Search sweep complete",
                keywords=keywords,
                radius=radius,
                max_price=max_price,
                min_price=min_price,
                location=location_slug,
                condition=condition,
                raw_results=raw_count,
                after_relevance_filter=len(listings),
            )
            return listings

        except Exception as exc:
            log.error("Search sweep failed", keywords=keywords, error=str(exc))
            return []
