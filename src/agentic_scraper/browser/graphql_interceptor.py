"""GraphQL network interceptor for Facebook Marketplace.

Captures listing data from Facebook's GraphQL API responses during
category page navigation. Uses CDP Network domain events to intercept
responses to /api/graphql/ and extract structured listing data.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from agentic_scraper.utils.logging import get_logger

if TYPE_CHECKING:
    from browser_use import BrowserSession

log = get_logger("browser.graphql_interceptor")


@dataclass
class GraphQLListingData:
    """Structured listing data extracted from a GraphQL response."""

    external_id: str = ""
    title: str = ""
    price: float | None = None
    currency: str = "USD"
    location: str = ""
    image_urls: list[str] = field(default_factory=list)
    listing_url: str = ""
    description: str = ""
    condition: str | None = None
    seller_name: str = ""
    posted_at: str | None = None  # ISO timestamp or unix timestamp string
    is_sponsored: bool = False
    category_id: str | None = None  # marketplace_listing_category_id from GraphQL
    delivery_types: list[str] = field(default_factory=list)  # ["IN_PERSON", "SHIPPING"]
    latitude: float | None = None  # From location object (if present in search results)
    longitude: float | None = None


@dataclass
class GraphQLPageResult:
    """Result of parsing a GraphQL response, including pagination info."""

    listings: list[GraphQLListingData]
    end_cursor: str | None = None
    has_next_page: bool = False


def parse_graphql_listings(body: str) -> list[GraphQLListingData]:
    """Parse Facebook GraphQL response JSON into structured listing data.

    Handles multiple known query response shapes with recursive fallback.

    Args:
        body: Raw JSON response body string.

    Returns:
        List of parsed listing data. Empty list on any parse failure.
    """
    return parse_graphql_response(body).listings


def parse_graphql_response(body: str) -> GraphQLPageResult:
    """Parse Facebook GraphQL response JSON into listings + pagination info.

    Same as parse_graphql_listings but also extracts the pagination cursor
    so callers can fetch subsequent pages.

    Args:
        body: Raw JSON response body string.

    Returns:
        GraphQLPageResult with listings and pagination cursor.
    """
    if not body:
        return GraphQLPageResult(listings=[])

    try:
        data = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return GraphQLPageResult(listings=[])

    if not isinstance(data, dict):
        return GraphQLPageResult(listings=[])

    listings: list[GraphQLListingData] = []
    seen_ids: set[str] = set()

    # Strategy 1: Known path for MarketplaceCategoryFeedQuery
    edges = _extract_edges(data)
    for edge in edges:
        listing_data = _parse_edge_node(edge)
        if listing_data and listing_data.external_id not in seen_ids:
            seen_ids.add(listing_data.external_id)
            listings.append(listing_data)

    # Extract pagination cursor from page_info
    end_cursor, has_next = _extract_page_info(data)

    if listings:
        return GraphQLPageResult(
            listings=listings,
            end_cursor=end_cursor,
            has_next_page=has_next,
        )

    # Strategy 2: Recursive search for listing-like dicts
    found = _recursive_find_listings(data)
    for item in found:
        if item.external_id not in seen_ids:
            seen_ids.add(item.external_id)
            listings.append(item)

    return GraphQLPageResult(
        listings=listings,
        end_cursor=end_cursor,
        has_next_page=has_next,
    )


def _extract_edges(data: dict) -> list[dict]:
    """Extract edge nodes from known GraphQL response structures."""
    # Path 1: data.marketplace_search.feed_units.edges
    try:
        edges = data["data"]["marketplace_search"]["feed_units"]["edges"]
        if isinstance(edges, list):
            return edges
    except (KeyError, TypeError):
        pass

    # Path 2: data.viewer.marketplace_feed_stories.edges
    try:
        edges = data["data"]["viewer"]["marketplace_feed_stories"]["edges"]
        if isinstance(edges, list):
            return edges
    except (KeyError, TypeError):
        pass

    # Path 3: data.marketplace_category_feed.edges (alternate name)
    try:
        edges = data["data"]["marketplace_category_feed"]["edges"]
        if isinstance(edges, list):
            return edges
    except (KeyError, TypeError):
        pass

    return []


def _extract_page_info(data: dict) -> tuple[str | None, bool]:
    """Extract pagination cursor from a GraphQL response.

    Facebook embeds page_info alongside the edges in feed_units (or similar).
    The end_cursor is an opaque string used to fetch the next page.

    Returns:
        Tuple of (end_cursor, has_next_page).
    """
    # All known paths where page_info lives (sibling to edges)
    _PAGE_INFO_PATHS: list[list[str]] = [
        ["data", "marketplace_search", "feed_units", "page_info"],
        ["data", "viewer", "marketplace_feed_stories", "page_info"],
        ["data", "marketplace_category_feed", "page_info"],
        # Alternate nesting seen in some responses
        ["data", "marketplace_search", "feed_units", "pageInfo"],
    ]

    for path in _PAGE_INFO_PATHS:
        obj: Any = data
        try:
            for key in path:
                obj = obj[key]
            if isinstance(obj, dict):
                cursor = obj.get("end_cursor") or obj.get("endCursor")
                has_next = obj.get("has_next_page", obj.get("hasNextPage", False))
                if cursor:
                    return str(cursor), bool(has_next)
        except (KeyError, TypeError, IndexError):
            continue

    return None, False


def _parse_edge_node(edge: dict) -> GraphQLListingData | None:
    """Parse a single edge node into GraphQLListingData."""
    try:
        node = edge.get("node", {})
        listing = node.get("listing", node)  # Some shapes nest under "listing"

        ext_id = str(listing.get("id", ""))
        if not ext_id:
            return None

        # Real Facebook listing IDs are pure numeric strings (e.g., "1234567890").
        # Non-listing nodes have compound IDs like
        # "19764:IN_MEMORY_MARKETPLACE_FEED_STORY_ENT:EntMarketplaceSearchFeedNoResults".
        # These are search metadata, "no results" placeholders, or feed story
        # wrappers — not actual marketplace listings.
        if not ext_id.isdigit():
            return None

        title = listing.get("marketplace_listing_title", "")

        # Guard: if there's no title AND no price fields, this isn't a real
        # listing node — it's likely search metadata that has a numeric `id`
        # but no listing data. Skip it.
        if not title and not listing.get("listing_price") and not listing.get("price"):
            return None

        # Price extraction — Facebook uses different price keys and formats
        # across endpoints. The anonymous GraphQL may use "listing_price",
        # "price", or nest the amount in different structures.
        price = None
        currency = "USD"
        # Try multiple known price field names
        for price_key in ("listing_price", "price", "formatted_price"):
            price_obj = listing.get(price_key)
            if price_obj and isinstance(price_obj, dict):
                try:
                    # Priority 1: "amount" field is in DOLLARS as a string
                    amount_str = price_obj.get("amount")
                    if not amount_str:
                        # Priority 2: offset fields are in CENTS — must divide by 100
                        cents_str = (
                            price_obj.get("amount_with_offset_in_currency")
                            or price_obj.get("amount_with_offset_amount")
                            or price_obj.get("amount_with_offset")
                        )
                        if cents_str:
                            try:
                                amount_str = str(float(str(cents_str)) / 100.0)
                            except (ValueError, TypeError):
                                amount_str = None
                    if not amount_str:
                        # Priority 3: formatted display string ("$18.00")
                        amount_str = (
                            price_obj.get("formatted_amount")
                            or price_obj.get("text", "")
                            or ""
                        )
                    amount_str = str(amount_str) if amount_str else ""
                    if amount_str:
                        # Strip currency symbols and parse
                        cleaned = amount_str.replace(",", "").replace("$", "").strip()
                        price = float(cleaned) if cleaned else None
                except (ValueError, TypeError):
                    price = None
                currency = price_obj.get("currency", "USD")
                if price is not None:
                    break
            elif price_obj and isinstance(price_obj, (int, float)):
                price = float(price_obj)
                break
            elif price_obj and isinstance(price_obj, str):
                # Price as plain string like "$25" or "25.00"
                try:
                    cleaned = price_obj.replace(",", "").replace("$", "").strip()
                    price = float(cleaned) if cleaned else None
                except (ValueError, TypeError):
                    pass
                if price is not None:
                    break

        # Image
        image_urls: list[str] = []
        photo = listing.get("primary_listing_photo")
        if photo and isinstance(photo, dict):
            img = photo.get("image", {})
            uri = img.get("uri", "")
            if uri:
                image_urls.append(uri)

        # Additional images
        photos = listing.get("listing_photos", [])
        if isinstance(photos, list):
            for p in photos:
                if isinstance(p, dict):
                    img = p.get("image", {})
                    uri = img.get("uri", "")
                    if uri and uri not in image_urls:
                        image_urls.append(uri)

        # Location (display name + coordinates if available)
        location = ""
        latitude = None
        longitude = None
        loc_obj = listing.get("location")
        if loc_obj and isinstance(loc_obj, dict):
            rg = loc_obj.get("reverse_geocode", {})
            cp = rg.get("city_page", {})
            location = cp.get("display_name", "")
            # Try extracting coordinates from location object (may not be in search results)
            try:
                lat_val = loc_obj.get("latitude")
                lon_val = loc_obj.get("longitude")
                if lat_val is not None and lon_val is not None:
                    latitude = float(lat_val)
                    longitude = float(lon_val)
            except (ValueError, TypeError):
                pass

        # Condition
        condition = listing.get("marketplace_listing_condition_type")

        # Timestamps
        posted_at = None
        creation_time = listing.get("creation_time")
        if creation_time:
            posted_at = str(creation_time)

        # Description
        description = listing.get("redacted_description", {})
        if isinstance(description, dict):
            description = description.get("text", "")
        elif not isinstance(description, str):
            description = ""

        # Seller
        seller_name = ""
        seller = listing.get("marketplace_listing_seller")
        if seller and isinstance(seller, dict):
            seller_name = seller.get("name", "")

        # Category ID — numeric category assigned by Facebook (100% reliable filtering)
        category_id = listing.get("marketplace_listing_category_id")
        if category_id is not None:
            category_id = str(category_id)

        # Delivery types (["IN_PERSON", "SHIPPING"])
        delivery_types = listing.get("delivery_types", [])
        if not isinstance(delivery_types, list):
            delivery_types = []

        # Sponsored / boosted listing detection (best-effort)
        is_sponsored = bool(listing.get("is_marketplace_boost_listing", False))
        tracking = listing.get("tracking")
        if not is_sponsored and tracking and isinstance(tracking, str):
            is_sponsored = "sponsor" in tracking.lower()

        return GraphQLListingData(
            external_id=ext_id,
            title=title,
            price=price,
            currency=currency,
            location=location,
            image_urls=image_urls,
            listing_url=f"https://www.facebook.com/marketplace/item/{ext_id}",
            description=description,
            condition=condition,
            seller_name=seller_name,
            posted_at=posted_at,
            is_sponsored=is_sponsored,
            category_id=category_id,
            delivery_types=delivery_types,
            latitude=latitude,
            longitude=longitude,
        )
    except Exception:
        return None


def _recursive_find_listings(obj: Any, depth: int = 0) -> list[GraphQLListingData]:
    """Recursively search for listing-like dicts in an arbitrary JSON structure.

    A dict is considered a listing if it has both an 'id' field and a
    'listing_price' or 'marketplace_listing_title' field.
    """
    if depth > 10:  # Prevent infinite recursion
        return []

    results: list[GraphQLListingData] = []

    if isinstance(obj, dict):
        # Check if this dict looks like a listing
        has_id = "id" in obj
        has_price = "listing_price" in obj
        has_title = "marketplace_listing_title" in obj

        if has_id and (has_price or has_title):
            # Treat this dict as a listing node (wrap in edge format)
            parsed = _parse_edge_node({"node": obj})
            if parsed:
                results.append(parsed)
                return results  # Don't recurse into a matched node

        # Recurse into values
        for v in obj.values():
            results.extend(_recursive_find_listings(v, depth + 1))

    elif isinstance(obj, list):
        for item in obj:
            results.extend(_recursive_find_listings(item, depth + 1))

    return results


class GraphQLInterceptor:
    """Captures Facebook Marketplace listing data from GraphQL network responses.

    Registers CDP Network event handlers to intercept GraphQL POST responses.
    When a response containing marketplace listing data is detected, parses the
    JSON and stores structured listing data.

    The interceptor is designed to capture data that Facebook sends anyway when
    a category page loads and scrolls — no extra requests are made.

    Usage:
        interceptor = GraphQLInterceptor()
        await interceptor.start(browser_session)
        # ... navigate and scroll ...
        listings = interceptor.drain()
        await interceptor.stop()
    """

    def __init__(self) -> None:
        self._captured: list[GraphQLListingData] = []
        self._seen_ids: set[str] = set()
        self._pending_requests: dict[str, str] = {}  # requestId -> URL
        self._session: BrowserSession | None = None
        self._session_id: str | None = None

    async def start(self, browser_session: BrowserSession) -> None:
        """Register CDP network event handlers.

        Args:
            browser_session: The browser-use BrowserSession to intercept.
        """
        self._session = browser_session

        try:
            cdp_session = await browser_session.get_or_create_cdp_session()
            self._session_id = cdp_session.session_id
            await cdp_session.cdp_client.send.Network.enable(
                session_id=cdp_session.session_id
            )

            cdp = browser_session.cdp_client.register
            cdp.Network.requestWillBeSent(self._on_request)
            cdp.Network.loadingFinished(self._on_loading_finished)

            log.info("GraphQL interceptor started")
        except Exception as exc:
            log.warning("Failed to start GraphQL interceptor", error=str(exc))

    def _on_request(self, params: dict, session_id: str | None) -> None:
        """Track POST requests to Facebook's GraphQL endpoint."""
        try:
            req = params.get("request", {})
            if isinstance(req, dict):
                url = req.get("url", "")
                method = req.get("method", "")
            else:
                url = getattr(req, "url", "")
                method = getattr(req, "method", "")

            request_id = (
                params.get("requestId")
                if isinstance(params, dict)
                else getattr(params, "requestId", None)
            )

            if not request_id:
                return

            # Only track GraphQL POSTs
            if "/api/graphql" in url and method == "POST":
                self._pending_requests[request_id] = url
        except Exception:
            pass

    def _on_loading_finished(self, params: dict, session_id: str | None) -> None:
        """Fetch the response body for tracked GraphQL requests."""
        try:
            request_id = (
                params.get("requestId")
                if isinstance(params, dict)
                else getattr(params, "requestId", None)
            )

            if not request_id or request_id not in self._pending_requests:
                return

            # Remove from pending
            self._pending_requests.pop(request_id, None)

            # Fetch response body asynchronously
            asyncio.create_task(self._fetch_and_parse(request_id, session_id))
        except Exception:
            pass

    async def _fetch_and_parse(
        self, request_id: str, session_id: str | None
    ) -> None:
        """Fetch a GraphQL response body and parse listings from it."""
        if not self._session:
            return

        try:
            resp = await self._session.cdp_client.send.Network.getResponseBody(
                params={"requestId": request_id},
                session_id=session_id,
            )
            body = resp.get("body", "")
            if resp.get("base64Encoded"):
                import base64

                body = base64.b64decode(body).decode("utf-8", errors="replace")

            if body:
                self._process_response_body(body)
        except Exception as exc:
            log.debug("Failed to fetch GraphQL response body", error=str(exc))

    def _process_response_body(self, body: str) -> None:
        """Parse listings from a GraphQL response body and add to captured list."""
        listings = parse_graphql_listings(body)
        for listing in listings:
            if listing.external_id and listing.external_id not in self._seen_ids:
                self._seen_ids.add(listing.external_id)
                self._captured.append(listing)

    def drain(self) -> list[GraphQLListingData]:
        """Return all captured listings and clear the buffer.

        Returns:
            List of captured listing data since last drain.
        """
        results = list(self._captured)
        self._captured.clear()
        return results

    async def stop(self) -> None:
        """Clean up interceptor state."""
        self._pending_requests.clear()
        self._session = None
        self._session_id = None
        log.info("GraphQL interceptor stopped")
