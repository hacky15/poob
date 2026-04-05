"""Anonymous Facebook Marketplace GraphQL client.

Makes direct POST requests to Facebook's GraphQL API without authentication.
Uses `__user=0` (anonymous viewer) to eliminate user-history personalization,
producing results based purely on location/query/recency.

Two-step approach for maximum compatibility:
1. GET facebook.com/marketplace to establish a `datr` cookie and extract `lsd` token
2. POST /api/graphql/ with session cookies + anonymous viewer fields

The response format is identical to what the browser's GraphQL interceptor
captures, so the existing `parse_graphql_listings()` parser works unchanged.

doc_ids are persisted query IDs that Facebook maps to pre-registered queries.
They change infrequently but can break on redeploy. When a doc_id breaks,
intercept a fresh one from browser DevTools.

References:
    - kyleronayne/marketplace-api (GitHub)
    - jongan69/fb-marketplace-api (GitHub)
    - Wes Bos gist (original __user=0 technique)
"""

from __future__ import annotations

import asyncio
import json
import random
import re
import time
from dataclasses import dataclass
from typing import Any

import httpx

# Check if brotli is available for httpx decompression
try:
    import brotli as _brotli  # noqa: F401
    _HAS_BROTLI = True
except ImportError:
    _HAS_BROTLI = False

from agentic_scraper.browser.graphql_interceptor import (
    GraphQLListingData,
    GraphQLPageResult,
    parse_graphql_listings,
    parse_graphql_response,
)
from agentic_scraper.utils.logging import get_logger

log = get_logger("sites.facebook.graphql_client")

# Known working doc_ids (updated from open-source projects, 2024-2025).
# These map to pre-registered GraphQL queries on Facebook's servers.
MARKETPLACE_SEARCH_DOC_ID = "7111939778879383"
LOCATION_SEARCH_DOC_ID = "5585904654783609"

# Approximate coordinates for known city slugs.
# Used when the client needs lat/lng from a city slug.
_CITY_COORDINATES: dict[str, tuple[float, float]] = {
    "appleton": (44.2619, -88.4154),
    "darboy": (44.2583, -88.3831),
    "madison": (43.0731, -89.4012),
    "green-bay": (44.5133, -88.0133),
    "oshkosh": (44.0247, -88.5426),
    "milwaukee": (43.0389, -87.9065),
    "neenah": (44.1858, -88.4626),
    "fond-du-lac": (43.7730, -88.4471),
}

# Default coordinates (Appleton, WI area) when no location specified.
DEFAULT_LATITUDE = 44.2619
DEFAULT_LONGITUDE = -88.4154


def resolve_center_coordinates(
    city_slug: str = "",
    explicit_lat: float = 0.0,
    explicit_lon: float = 0.0,
) -> tuple[float, float]:
    """Resolve search center coordinates from config.

    Priority: explicit lat/lon > city slug lookup > default (Appleton, WI).
    """
    if explicit_lat != 0.0 and explicit_lon != 0.0:
        return (explicit_lat, explicit_lon)
    if city_slug and city_slug.lower() in _CITY_COORDINATES:
        return _CITY_COORDINATES[city_slug.lower()]
    return (DEFAULT_LATITUDE, DEFAULT_LONGITUDE)

# User-Agent rotation pool (browser-like, updated 2026).
_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:135.0) Gecko/20100101 Firefox/135.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
]

# Regex patterns for extracting tokens from Facebook page HTML.
# Facebook changes these formats periodically — multiple patterns for resilience.
_LSD_PATTERNS = [
    re.compile(r'name="lsd"\s+value="([^"]+)"'),             # Hidden form field (legacy)
    re.compile(r'\["LSD",\[],\{"token":"([^"]+?)"\}'),       # ScheduledServerJS / Comet SSR
    re.compile(r'"LSD",\[\],\{"token":"([^"]+)"\}'),         # ServerJSDefine (older)
    re.compile(r'"lsd"\s*:\s*"([^"]+)"'),                    # Generic JSON key
]
_DATR_PATTERN = re.compile(r'"_js_datr","([^"]+)"')

# Static fallback lsd token — some Facebook endpoints accept any non-empty value
# for anonymous requests. Used when extraction fails.
_FALLBACK_LSD = "AVqbxe3J_YA"


@dataclass
class GraphQLSearchParams:
    """Parameters for a Facebook Marketplace GraphQL search.

    Args:
        query: Search keywords (empty string for browse/category).
        latitude: Search center latitude.
        longitude: Search center longitude.
        radius_km: Search radius in kilometers.
        count: Number of results per page.
        cursor: Pagination cursor (None for first page).
        min_price: Minimum price in cents (0 for no minimum).
        max_price: Maximum price in cents (214748364700 for no maximum).
        days_listed: Filter by listing age in days (None for any).
        condition: Item condition filter (None for any).
    """

    query: str = ""
    latitude: float = DEFAULT_LATITUDE
    longitude: float = DEFAULT_LONGITUDE
    radius_km: int = 64  # ~40 miles
    count: int = 50
    cursor: str | None = None
    min_price: int = 0
    max_price: int = 214748364700  # Effectively unlimited
    days_listed: int | None = 1
    condition: str | None = None


class AnonymousGraphQLClient:
    """Makes anonymous GraphQL requests to Facebook Marketplace.

    Two-step approach:
    1. Bootstrap: GET /marketplace/ to get `datr` cookie + `lsd` token
    2. Query: POST /api/graphql/ with `__user=0` (anonymous viewer)

    This eliminates user-history personalization from search results.
    Only IP/location signals remain.

    Features:
        - Session bootstrapping (datr cookie + lsd token)
        - User-Agent rotation (consistent per session)
        - Rate limiting with configurable delay
        - Automatic retry with backoff on 429
        - Pagination support via cursors
        - doc_id staleness detection
        - Fallback: can use browser cookies if bootstrapping fails

    Args:
        min_delay_seconds: Minimum delay between requests (default 12s).
        max_retries: Maximum retries on rate limit (default 3).
        timeout_seconds: HTTP request timeout (default 15s).
    """

    ENDPOINT = "https://www.facebook.com/api/graphql/"
    MARKETPLACE_URL = "https://www.facebook.com/marketplace/"

    def __init__(
        self,
        *,
        min_delay_seconds: float = 12.0,
        max_retries: int = 3,
        timeout_seconds: float = 15.0,
    ) -> None:
        self._min_delay = min_delay_seconds
        self._max_retries = max_retries
        self._timeout = timeout_seconds
        self._last_request_time: float = 0.0
        self._client: httpx.AsyncClient | None = None
        self._doc_id_broken = False
        # Session state from bootstrapping
        self._lsd_token: str | None = None
        self._session_ua: str = random.choice(_USER_AGENTS)
        self._bootstrapped = False
        self._bootstrap_failed = False
        # Optional: browser-extracted cookies for fallback
        self._browser_cookies: dict[str, str] | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create the shared httpx client with cookie persistence."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=self._timeout,
                follow_redirects=True,
            )
        return self._client

    async def bootstrap_session(self) -> bool:
        """Bootstrap an anonymous session by visiting the marketplace page.

        Makes a GET request to facebook.com/marketplace to:
        1. Receive the `datr` cookie (device tracking, set via Set-Cookie)
        2. Extract the `lsd` token from page HTML (CSRF protection)

        These are then included in subsequent GraphQL POST requests to make
        them look like real browser requests from an anonymous visitor.

        Returns:
            True if session was bootstrapped successfully.
        """
        try:
            client = await self._get_client()
            headers = {
                "User-Agent": self._session_ua,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Accept-Encoding": "gzip, deflate, br" if _HAS_BROTLI else "gzip, deflate",
                "sec-fetch-site": "none",
                "sec-fetch-mode": "navigate",
                "sec-fetch-dest": "document",
                "sec-fetch-user": "?1",
                "Upgrade-Insecure-Requests": "1",
            }

            response = await client.get(self.MARKETPLACE_URL, headers=headers)

            if response.status_code != 200:
                log.warning(
                    "graphql_client.bootstrap_http_error",
                    status=response.status_code,
                )
                self._bootstrap_failed = True
                return False

            html = response.text

            # Extract lsd token from HTML — try all known patterns
            for pattern in _LSD_PATTERNS:
                lsd_match = pattern.search(html)
                if lsd_match:
                    self._lsd_token = lsd_match.group(1)
                    break

            # Use static fallback if extraction failed — anonymous GraphQL
            # works without lsd on many endpoints, and some accept any value.
            if not self._lsd_token:
                self._lsd_token = _FALLBACK_LSD
                log.debug(
                    "graphql_client.lsd_extraction_failed_using_fallback",
                    html_length=len(html),
                )

            # Check if datr cookie was set (httpx stores it automatically)
            has_datr = any(
                "datr" in str(cookie) for cookie in client.cookies.jar
            )

            self._bootstrapped = True
            log.info(
                "graphql_client.session_bootstrapped",
                has_lsd=self._lsd_token is not None,
                lsd_source="extracted" if self._lsd_token != _FALLBACK_LSD else "fallback",
                has_datr=has_datr,
                cookies=len(client.cookies.jar),
            )
            return True

        except Exception as exc:
            log.warning(
                "graphql_client.bootstrap_failed",
                error=str(exc)[:200],
            )
            self._bootstrap_failed = True
            return False

    def set_browser_cookies(self, cookies: dict[str, str]) -> None:
        """Inject cookies extracted from the browser session.

        Used as a fallback when session bootstrapping fails.
        Only anonymous-safe cookies should be passed (datr, sb).
        Do NOT pass authentication cookies (c_user, xs).

        Args:
            cookies: Dict of cookie name → value.
        """
        self._browser_cookies = cookies
        log.info(
            "graphql_client.browser_cookies_set",
            cookie_names=list(cookies.keys()),
        )

    def _build_headers(self) -> dict[str, str]:
        """Build request headers mimicking a browser."""
        headers = {
            "User-Agent": self._session_ua,
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br" if _HAS_BROTLI else "gzip, deflate",
            "Content-Type": "application/x-www-form-urlencoded",
            "sec-fetch-site": "same-origin",
            "sec-fetch-mode": "cors",
            "sec-fetch-dest": "empty",
            "Referer": "https://www.facebook.com/marketplace/",
            "Origin": "https://www.facebook.com",
        }
        # Add lsd as header too (some FB endpoints check this)
        if self._lsd_token:
            headers["x-fb-lsd"] = self._lsd_token
        return headers

    def _build_body(self, params: GraphQLSearchParams) -> dict[str, str]:
        """Build the full POST body with anonymous viewer fields."""
        variables = self._build_variables(params)

        body: dict[str, str] = {
            "variables": json.dumps(variables),
            "doc_id": MARKETPLACE_SEARCH_DOC_ID,
            # Anonymous viewer fields — key to depersonalization
            "__user": "0",
            "__a": "1",
            "av": "0",
            # GraphQL client metadata
            "fb_api_caller_class": "RelayModern",
            "fb_api_req_friendly_name": "CometMarketplaceSearchContentContainerQuery",
            "server_timestamps": "true",
        }

        # Include lsd token if we have it (CSRF protection)
        if self._lsd_token:
            body["lsd"] = self._lsd_token

        return body

    def _build_variables(self, params: GraphQLSearchParams) -> dict[str, Any]:
        """Build the GraphQL variables JSON for a marketplace search."""
        browse_params: dict[str, Any] = {
            "commerce_enable_local_pickup": True,
            "commerce_enable_shipping": True,
            "commerce_search_and_rp_available": True,
            "commerce_search_and_rp_condition": params.condition,
            "commerce_search_and_rp_ctime_days": params.days_listed,
            "filter_location_latitude": params.latitude,
            "filter_location_longitude": params.longitude,
            "filter_price_lower_bound": params.min_price,
            "filter_price_upper_bound": params.max_price,
            "filter_radius_km": params.radius_km,
        }

        variables: dict[str, Any] = {
            "count": params.count,
            "params": {
                "bqf": {
                    "callsite": "COMMERCE_MKTPLACE_WWW",
                    "query": params.query,
                },
                "browse_request_params": browse_params,
                "custom_request_params": {
                    "surface": "SEARCH",
                },
            },
        }

        if params.cursor:
            variables["cursor"] = params.cursor

        return variables

    async def _rate_limit_wait(self) -> None:
        """Wait to respect rate limiting between requests."""
        elapsed = time.monotonic() - self._last_request_time
        if elapsed < self._min_delay:
            wait = self._min_delay - elapsed + random.uniform(0, 1.0)
            await asyncio.sleep(wait)

    async def _ensure_session(self) -> None:
        """Ensure we have a bootstrapped session before making queries."""
        if self._bootstrapped or self._bootstrap_failed:
            return

        # Apply browser cookies if provided (fallback path)
        if self._browser_cookies:
            client = await self._get_client()
            for name, value in self._browser_cookies.items():
                client.cookies.set(name, value, domain=".facebook.com")
            self._bootstrapped = True
            return

        await self.bootstrap_session()

    async def search(
        self, params: GraphQLSearchParams
    ) -> list[GraphQLListingData]:
        """Execute a marketplace search via anonymous GraphQL.

        Automatically bootstraps a session on first call.

        Args:
            params: Search parameters.

        Returns:
            List of parsed listing data. Empty on failure.
        """
        return (await self._search_with_cursor(params)).listings

    async def _search_with_cursor(
        self, params: GraphQLSearchParams
    ) -> GraphQLPageResult:
        """Execute a marketplace search and return listings + pagination cursor.

        Internal method used by both search() and search_all_pages().

        Args:
            params: Search parameters.

        Returns:
            GraphQLPageResult with listings and cursor for next page.
        """
        if self._doc_id_broken:
            log.debug("graphql_client.skip_broken_doc_id")
            return GraphQLPageResult(listings=[])

        await self._ensure_session()

        body = self._build_body(params)

        for attempt in range(self._max_retries + 1):
            await self._rate_limit_wait()

            try:
                client = await self._get_client()
                t0 = time.monotonic()

                response = await client.post(
                    self.ENDPOINT,
                    data=body,
                    headers=self._build_headers(),
                )
                self._last_request_time = time.monotonic()
                elapsed = time.monotonic() - t0

                # Check for rate limiting
                if response.status_code == 429:
                    retry_delay = 120.0 * (attempt + 1)
                    log.warning(
                        "graphql_client.rate_limited",
                        attempt=attempt,
                        retry_delay=retry_delay,
                    )
                    await asyncio.sleep(retry_delay)
                    continue

                # Check for HTML response (CAPTCHA / blocking)
                content_type = response.headers.get("content-type", "")
                if "text/html" in content_type:
                    html_preview = response.text[:500].replace("\n", " ")
                    is_login = "login" in html_preview.lower()
                    is_captcha = "captcha" in html_preview.lower()
                    # Some Facebook responses mix HTML with JSON —
                    # try to extract JSON from the response body
                    text = response.text.strip()
                    if text.startswith("for (;;);"):
                        text = text[len("for (;;);"):]
                    if text.startswith("{") or text.startswith("["):
                        # Looks like JSON despite HTML content-type —
                        # Facebook often returns GraphQL JSON with text/html content-type
                        try:
                            page_result = parse_graphql_response(text)
                            if page_result.listings:
                                log.info(
                                    "graphql_client.json_in_html_response",
                                    count=len(page_result.listings),
                                )
                                return page_result
                            # Valid GraphQL response but empty edges — this is
                            # "no more results", not an error. Return empty result
                            # so pagination stops cleanly instead of retrying.
                            if '"edges":[]' in text or '"edges": []' in text:
                                log.info(
                                    "graphql_client.empty_graphql_page",
                                    hint="valid response with no listings",
                                )
                                return page_result
                        except Exception:
                            pass
                    log.warning(
                        "graphql_client.html_response",
                        status=response.status_code,
                        is_login_redirect=is_login,
                        is_captcha=is_captcha,
                        bootstrapped=self._bootstrapped,
                        lsd_source="extracted" if self._lsd_token != _FALLBACK_LSD else "fallback",
                        html_snippet=html_preview[:200],
                        hint="IP block or session rejection",
                    )
                    # Retry once with re-bootstrapped session if first attempt
                    if attempt == 0 and not self._bootstrap_failed:
                        log.info("graphql_client.retry_with_fresh_session")
                        self._bootstrapped = False
                        self._lsd_token = None
                        await self.bootstrap_session()
                        body = self._build_body(params)
                        continue
                    return GraphQLPageResult(listings=[])

                if response.status_code != 200:
                    log.warning(
                        "graphql_client.http_error",
                        status=response.status_code,
                        body_preview=response.text[:200],
                    )
                    return GraphQLPageResult(listings=[])

                # Parse the response — strip Facebook's anti-XSSI prefix
                response_text = response.text.strip()
                if response_text.startswith("for (;;);"):
                    response_text = response_text[len("for (;;);"):]
                page_result = parse_graphql_response(response_text)

                # Detect broken doc_id: valid HTTP but error in JSON
                if not page_result.listings:
                    try:
                        resp_json = json.loads(response_text)
                        errors = resp_json.get("errors") or resp_json.get("error")
                        if errors:
                            log.warning(
                                "graphql_client.api_error",
                                errors=str(errors)[:200],
                                hint="doc_id may be stale",
                            )
                            self._doc_id_broken = True
                            return GraphQLPageResult(listings=[])
                    except (json.JSONDecodeError, ValueError):
                        pass

                log.info(
                    "graphql_client.search_success",
                    query=params.query[:50] or "(browse)",
                    listings=len(page_result.listings),
                    elapsed_s=round(elapsed, 1),
                    radius_km=params.radius_km,
                    has_next_page=page_result.has_next_page,
                    bootstrapped=self._bootstrapped,
                )
                return page_result

            except httpx.TimeoutException:
                log.warning(
                    "graphql_client.timeout",
                    attempt=attempt,
                    timeout=self._timeout,
                )
            except Exception as exc:
                log.warning(
                    "graphql_client.error",
                    attempt=attempt,
                    error=str(exc)[:200],
                )

        return GraphQLPageResult(listings=[])

    async def search_all_pages(
        self,
        params: GraphQLSearchParams,
        *,
        max_pages: int = 4,
        known_ids: set[str] | None = None,
    ) -> list[GraphQLListingData]:
        """Search with automatic cursor-based pagination.

        Fetches up to max_pages of results by following the end_cursor
        from each GraphQL response. This is how Facebook's own infinite
        scroll works — each page returns ~24-50 items and a cursor for
        the next batch.

        Args:
            params: Search parameters (cursor will be managed automatically).
            max_pages: Maximum number of pages to fetch (default 4 = ~200 listings).
            known_ids: External IDs already in the DB. When >80% of a page
                is known, pagination stops early to avoid wasting time
                fetching listings we'll discard in dedup.

        Returns:
            Combined listings from all pages, deduplicated.
        """
        all_listings: list[GraphQLListingData] = []
        seen_ids: set[str] = set()
        current_params = GraphQLSearchParams(
            query=params.query,
            latitude=params.latitude,
            longitude=params.longitude,
            radius_km=params.radius_km,
            count=params.count,
            min_price=params.min_price,
            max_price=params.max_price,
            days_listed=params.days_listed,
            condition=params.condition,
        )

        for page_num in range(max_pages):
            page_result = await self._search_with_cursor(current_params)
            if not page_result.listings:
                log.info(
                    "graphql_client.pagination_stopped",
                    reason="empty_page",
                    page=page_num + 1,
                    total_so_far=len(all_listings),
                )
                break

            new_on_page = 0
            for listing in page_result.listings:
                if listing.external_id not in seen_ids:
                    seen_ids.add(listing.external_id)
                    all_listings.append(listing)
                    new_on_page += 1

            log.info(
                "graphql_client.page_fetched",
                page=page_num + 1,
                new_listings=new_on_page,
                total_so_far=len(all_listings),
                has_next=page_result.has_next_page,
                has_cursor=page_result.end_cursor is not None,
            )

            # Early exit: if >80% of this page is already in the DB, deeper
            # pages will be even staler. Stop fetching to save time.
            # Checks every page including page 1 — on cycle 2+ most page 1
            # results are already known, so we skip page 2 entirely.
            if known_ids and page_result.listings:
                total_on_page = len(page_result.listings)
                known_on_page = sum(
                    1 for l in page_result.listings
                    if l.external_id in known_ids
                )
                if total_on_page > 0 and known_on_page / total_on_page > 0.8:
                    log.info(
                        "graphql_client.pagination_stopped",
                        reason="high_dedup_rate",
                        page=page_num + 1,
                        known_pct=round(known_on_page / total_on_page * 100),
                        total_so_far=len(all_listings),
                    )
                    break

            # Stop if no cursor or Facebook says no more pages
            if not page_result.end_cursor or not page_result.has_next_page:
                break

            # Set cursor for next page
            current_params = GraphQLSearchParams(
                query=params.query,
                latitude=params.latitude,
                longitude=params.longitude,
                radius_km=params.radius_km,
                count=params.count,
                cursor=page_result.end_cursor,
                min_price=params.min_price,
                max_price=params.max_price,
                days_listed=params.days_listed,
                condition=params.condition,
            )

        log.info(
            "graphql_client.pagination_complete",
            query=params.query[:50] or "(browse)",
            total_listings=len(all_listings),
        )
        return all_listings

    async def close(self) -> None:
        """Close the HTTP client."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    @property
    def is_doc_id_broken(self) -> bool:
        """Whether the current doc_id appears to be stale."""
        return self._doc_id_broken

    def reset_doc_id_state(self) -> None:
        """Reset the doc_id broken flag (e.g., after manual update)."""
        self._doc_id_broken = False

    def reset_session(self) -> None:
        """Reset session state to force re-bootstrapping."""
        self._bootstrapped = False
        self._bootstrap_failed = False
        self._lsd_token = None
        self._session_ua = random.choice(_USER_AGENTS)


def miles_to_km(miles: int) -> int:
    """Convert miles to kilometers (rounded)."""
    return round(miles * 1.60934)


def build_search_params(
    *,
    query: str = "",
    location_slug: str | None = None,
    latitude: float | None = None,
    longitude: float | None = None,
    radius_miles: int = 40,
    max_price: float | None = None,
    min_price: float | None = None,
    days_listed: int = 1,
    condition: str | None = None,
    count: int = 50,
) -> GraphQLSearchParams:
    """Build search params from user-facing values.

    Handles city slug → coordinates lookup and miles → km conversion.

    Args:
        query: Search keywords.
        location_slug: City slug (e.g., "madison", "appleton").
        latitude: Explicit latitude (overrides location_slug).
        longitude: Explicit longitude (overrides location_slug).
        radius_miles: Search radius in miles.
        max_price: Maximum price in dollars (None for no limit).
        min_price: Minimum price in dollars (None for no minimum).
        days_listed: Filter by listing age in days.
        condition: Item condition filter.
        count: Results per page.

    Returns:
        GraphQLSearchParams ready for the client.
    """
    # Resolve coordinates
    if latitude is not None and longitude is not None:
        lat, lng = latitude, longitude
    elif location_slug and location_slug.lower() in _CITY_COORDINATES:
        lat, lng = _CITY_COORDINATES[location_slug.lower()]
    else:
        lat, lng = DEFAULT_LATITUDE, DEFAULT_LONGITUDE

    return GraphQLSearchParams(
        query=query,
        latitude=lat,
        longitude=lng,
        radius_km=miles_to_km(radius_miles),
        count=count,
        min_price=int(min_price * 100) if min_price else 0,
        max_price=int(max_price * 100) if max_price else 214748364700,
        days_listed=days_listed,
        condition=condition,
    )
