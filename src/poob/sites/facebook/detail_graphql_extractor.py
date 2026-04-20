"""GraphQL-based detail page extractor for Facebook Marketplace.

Facebook's React/Relay SPA injects listing data via:
1. data-sjs script tags containing ScheduledServerJS payloads with GraphQL data
2. DOM structural elements rendered by React from that data

OG meta tags and JSON-LD do NOT work (see docs/research/fb-detail-page-extraction.md).

This module extracts listing details from those GraphQL payloads and DOM elements.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from poob.utils.logging import get_logger

log = get_logger("sites.facebook.detail_graphql_extractor")


@dataclass
class DetailPageData:
    """Structured data extracted from a Facebook Marketplace detail page."""

    title: str = ""
    description: str = ""
    price: float | None = None
    currency: str = "USD"
    condition: str = ""
    location: str = ""
    seller_name: str = ""
    posted_at: datetime | None = None
    image_urls: list[str] | None = None
    latitude: float | None = None
    longitude: float | None = None
    is_sold: bool = False
    is_pending: bool = False
    strikethrough_price: float | None = None


# JavaScript to extract ALL data-sjs script tag contents that mention marketplace.
# Returns a list of JSON strings containing marketplace data.
EXTRACT_DATA_SJS_JS = """() => {
    const results = [];
    const scripts = document.querySelectorAll('script[type="application/json"][data-sjs]');
    for (const script of scripts) {
        const text = script.textContent || '';
        if (text.includes('marketplace_listing_title')
            || text.includes('marketplace_product_details')
            || text.includes('MarketplaceListing')
            || text.includes('listing_price')
            || text.includes('redacted_description')) {
            results.push(text);
        }
    }
    return results;
}"""

# DOM structural extraction as Tier 3 fallback.
# Uses the most stable selectors based on accessibility compliance.
EXTRACT_DOM_DETAIL_JS = """() => {
    const result = {};

    // Title: first h1 with dir="auto" span
    const h1 = document.querySelector('h1 span[dir="auto"]');
    if (h1) result.title = h1.textContent.trim();

    // Price: look for spans with $ near the title area
    const mainContent = document.querySelector('[role="main"]');
    if (mainContent) {
        const spans = mainContent.querySelectorAll('span[dir="auto"]');
        for (const span of spans) {
            const text = span.textContent.trim();
            if (/^\\$[\\d,]+(\\.\\d{2})?$/.test(text) || text === 'Free') {
                result.price_text = text;
                break;
            }
        }

        // Description: longer text blocks within main content
        const textBlocks = mainContent.querySelectorAll('span[dir="auto"]');
        let longestBlock = '';
        for (const block of textBlocks) {
            const text = block.textContent.trim();
            if (text.length > longestBlock.length && text.length > 50) {
                longestBlock = text;
            }
        }
        if (longestBlock) result.description = longestBlock;
    }

    // Images: all scontent images in the main area
    const images = [];
    const imgs = document.querySelectorAll('[role="main"] img[src*="scontent"]');
    for (const img of imgs) {
        if (img.src && !images.includes(img.src) && img.width > 100) {
            images.push(img.src);
        }
    }
    if (images.length) result.image_urls = images;

    // Seller name: link to user profile
    const sellerLink = document.querySelector('a[href*="/user/"] span[dir="auto"]');
    if (sellerLink) result.seller_name = sellerLink.textContent.trim();

    // Location text
    const locationSpans = document.querySelectorAll('span[dir="auto"]');
    for (const span of locationSpans) {
        const text = span.textContent.trim();
        // Location patterns: "City, State" or "Listed in City"
        if (/^Listed in /i.test(text)) {
            result.location = text.replace(/^Listed in /i, '').trim();
            break;
        }
        if (/^[A-Z][a-z]+,\\s*[A-Z]{2}$/.test(text)) {
            result.location = text;
            break;
        }
    }

    // Freshness badge
    const allText = mainContent ? mainContent.innerText : '';
    const freshMatch = allText.match(
        /(Just listed|Listed (?:\\d+ (?:minutes?|hours?|days?|weeks?) ago|yesterday))/i
    );
    if (freshMatch) result.freshness = freshMatch[0];

    // Condition
    const condMatch = allText.match(/(New|Used - Like new|Used - Good|Used - Fair)/i);
    if (condMatch) result.condition = condMatch[0];

    return result;
}"""


def parse_data_sjs_payloads(payloads: list[str]) -> DetailPageData:
    """Parse Facebook data-sjs script contents for marketplace listing data.

    Searches through ScheduledServerJS payloads for GraphQL data containing
    marketplace listing fields. Uses regex for resilience against JSON nesting.

    Args:
        payloads: List of JSON strings from data-sjs script tags.

    Returns:
        DetailPageData with whatever fields could be extracted.
    """
    result = DetailPageData()

    for payload_str in payloads:
        try:
            _extract_from_payload_text(payload_str, result)
        except Exception as exc:
            log.debug("data-sjs parse error", error=str(exc)[:100])
            continue

        # Also try to parse as JSON and walk the structure
        try:
            data = json.loads(payload_str)
            _walk_json_for_listing(data, result)
        except (json.JSONDecodeError, ValueError):
            pass

    return result


def _extract_from_payload_text(text: str, result: DetailPageData) -> None:
    """Extract fields from raw payload text using regex.

    This is more resilient than JSON walking because Facebook's payloads
    use deeply nested, version-specific structures. Regex finds the values
    regardless of nesting depth.
    """
    # Title
    if not result.title:
        m = re.search(r'"marketplace_listing_title"\s*:\s*"((?:[^"\\]|\\.)*)"', text)
        if not m:
            m = re.search(r'"base_marketplace_listing_title"\s*:\s*"((?:[^"\\]|\\.)*)"', text)
        if m:
            result.title = _unescape_json_string(m.group(1))

    # Description
    if not result.description:
        m = re.search(r'"redacted_description"\s*:\s*\{[^}]*"text"\s*:\s*"((?:[^"\\]|\\.)*)"', text)
        if m:
            result.description = _unescape_json_string(m.group(1))

    # Price (dollars string from "amount" field)
    if result.price is None:
        m = re.search(r'"listing_price"\s*:\s*\{[^}]*"amount"\s*:\s*"([\d.]+)"', text)
        if m:
            try:
                result.price = float(m.group(1))
            except ValueError:
                pass

    # Price fallback: formatted_amount
    if result.price is None:
        m = re.search(r'"formatted_amount"\s*:\s*"(\$?[\d,]+\.?\d*)"', text)
        if m:
            try:
                result.price = float(m.group(1).replace("$", "").replace(",", ""))
            except ValueError:
                pass

    # Strikethrough price (original price if reduced)
    if result.strikethrough_price is None:
        m = re.search(
            r'"strikethrough_price"\s*:\s*\{[^}]*"formatted_amount"\s*:\s*"(\$?[\d,]+\.?\d*)"',
            text,
        )
        if m:
            try:
                result.strikethrough_price = float(
                    m.group(1).replace("$", "").replace(",", "")
                )
            except ValueError:
                pass

    # Creation time (Unix timestamp)
    if result.posted_at is None:
        m = re.search(r'"creation_time"\s*:\s*(\d{10,})', text)
        if m:
            try:
                result.posted_at = datetime.fromtimestamp(int(m.group(1)), tz=timezone.utc)
            except (ValueError, OSError, OverflowError):
                pass

    # Condition
    if not result.condition:
        m = re.search(
            r'"marketplace_listing_condition_type"\s*:\s*"([^"]+)"'
            r'|"condition"\s*:\s*"([^"]+)"',
            text,
        )
        if m:
            result.condition = m.group(1) or m.group(2) or ""

    # Seller name
    if not result.seller_name:
        m = re.search(r'"marketplace_listing_seller"\s*:\s*\{[^}]*"name"\s*:\s*"((?:[^"\\]|\\.)*)"', text)
        if m:
            result.seller_name = _unescape_json_string(m.group(1))

    # Location text
    if not result.location:
        m = re.search(r'"location_text"\s*:\s*"((?:[^"\\]|\\.)*)"', text)
        if m:
            result.location = _unescape_json_string(m.group(1))

    # Image URLs from listing_photos.
    # Cap at 8 — Facebook payloads can contain images from related/recommended
    # listings in the same response, and we only want THIS listing's photos.
    if not result.image_urls:
        urls: list[str] = []
        for m in re.finditer(r'"uri"\s*:\s*"(https://[^"]*scontent[^"]*)"', text):
            url = m.group(1)
            if url not in urls:
                urls.append(url)
                if len(urls) >= 8:
                    break
        if urls:
            result.image_urls = urls

    # is_sold / is_pending
    if re.search(r'"is_sold"\s*:\s*true', text, re.IGNORECASE):
        result.is_sold = True
    if re.search(r'"is_pending"\s*:\s*true', text, re.IGNORECASE):
        result.is_pending = True

    # GPS coordinates
    if result.latitude is None:
        m = re.search(r'"latitude"\s*:\s*([-\d.]+)', text)
        if m:
            try:
                result.latitude = float(m.group(1))
            except ValueError:
                pass
    if result.longitude is None:
        m = re.search(r'"longitude"\s*:\s*([-\d.]+)', text)
        if m:
            try:
                result.longitude = float(m.group(1))
            except ValueError:
                pass

    # Currency
    m = re.search(r'"currency"\s*:\s*"([A-Z]{3})"', text)
    if m:
        result.currency = m.group(1)


def _walk_json_for_listing(obj: Any, result: DetailPageData, depth: int = 0) -> bool:
    """Walk parsed JSON looking for listing nodes to fill DetailPageData.

    Returns True if we found and populated listing data at this level.
    """
    if depth > 12:
        return False

    if isinstance(obj, dict):
        # Check if this dict looks like a listing node
        if "marketplace_listing_title" in obj and not result.title:
            result.title = obj["marketplace_listing_title"]

        if "redacted_description" in obj and not result.description:
            desc = obj["redacted_description"]
            if isinstance(desc, dict):
                result.description = desc.get("text", "")

        if "listing_price" in obj and result.price is None:
            _extract_price_from_obj(obj["listing_price"], result)

        if "creation_time" in obj and result.posted_at is None:
            try:
                ct = obj["creation_time"]
                result.posted_at = datetime.fromtimestamp(int(ct), tz=timezone.utc)
            except (ValueError, TypeError, OSError, OverflowError):
                pass

        if "marketplace_listing_seller" in obj and not result.seller_name:
            seller = obj["marketplace_listing_seller"]
            if isinstance(seller, dict):
                result.seller_name = seller.get("name", "")

        if "location_text" in obj and not result.location:
            result.location = obj["location_text"]

        if "marketplace_listing_condition_type" in obj and not result.condition:
            result.condition = obj["marketplace_listing_condition_type"]

        # Recurse
        for v in obj.values():
            _walk_json_for_listing(v, result, depth + 1)

    elif isinstance(obj, list):
        for item in obj:
            _walk_json_for_listing(item, result, depth + 1)

    return bool(result.title or result.description)


def _extract_price_from_obj(price_obj: Any, result: DetailPageData) -> None:
    """Extract price from a listing_price dict, handling dollars vs cents."""
    if not isinstance(price_obj, dict):
        return

    # Priority 1: "amount" is in DOLLARS as a string
    amount = price_obj.get("amount")
    if amount:
        try:
            result.price = float(str(amount))
            return
        except (ValueError, TypeError):
            pass

    # Priority 2: offset fields are in CENTS — divide by 100
    for cents_key in (
        "amount_with_offset_in_currency",
        "amount_with_offset_amount",
        "amount_with_offset",
    ):
        cents = price_obj.get(cents_key)
        if cents:
            try:
                result.price = float(str(cents)) / 100.0
                return
            except (ValueError, TypeError):
                pass

    # Priority 3: formatted display string
    formatted = price_obj.get("formatted_amount") or price_obj.get("text")
    if formatted:
        try:
            cleaned = str(formatted).replace(",", "").replace("$", "").strip()
            result.price = float(cleaned) if cleaned else None
        except (ValueError, TypeError):
            pass

    result.currency = price_obj.get("currency", result.currency)


def _unescape_json_string(s: str) -> str:
    """Unescape a JSON string that was extracted via regex (not json.loads)."""
    return (
        s.replace('\\"', '"')
        .replace("\\n", "\n")
        .replace("\\t", "\t")
        .replace("\\/", "/")
        .replace("\\\\", "\\")
    )


def parse_dom_detail(dom_data: dict) -> DetailPageData:
    """Parse DOM extraction results into DetailPageData.

    Args:
        dom_data: Dict from EXTRACT_DOM_DETAIL_JS evaluation.

    Returns:
        DetailPageData populated from DOM elements.
    """
    result = DetailPageData()

    result.title = dom_data.get("title", "")
    result.description = dom_data.get("description", "")
    result.seller_name = dom_data.get("seller_name", "")
    result.location = dom_data.get("location", "")
    result.condition = dom_data.get("condition", "")

    # Parse price text
    price_text = dom_data.get("price_text", "")
    if price_text == "Free":
        result.price = 0.0
    elif price_text:
        try:
            result.price = float(price_text.replace("$", "").replace(",", ""))
        except (ValueError, TypeError):
            pass

    # Parse images
    if dom_data.get("image_urls"):
        result.image_urls = dom_data["image_urls"]

    # Parse freshness
    freshness = dom_data.get("freshness", "")
    if freshness:
        try:
            from poob.sites.facebook.parser import _parse_freshness

            result.posted_at = _parse_freshness(freshness)
        except Exception:
            pass

    return result
