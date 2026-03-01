"""Detail page extractor for Facebook Marketplace listings.

Navigates to individual listing pages and extracts enriched data
from Open Graph meta tags and JSON-LD structured data embedded
in the HTML <head>, without requiring LLM assistance.
"""

from __future__ import annotations

from dataclasses import replace

from agentic_scraper.browser.page_actions import navigate_and_wait
from agentic_scraper.sites.facebook.js_extractor import (
    EXTRACT_JSON_LD_JS,
    EXTRACT_OPEN_GRAPH_JS,
)
from agentic_scraper.storage.models import Listing
from agentic_scraper.utils.logging import get_logger

log = get_logger("sites.facebook.detail_extractor")


async def extract_listing_details(page: object, listing: Listing) -> Listing:
    """Navigate to a listing page and enrich it with OG + JSON-LD data.

    Uses Open Graph meta tags for title, description, and image,
    and JSON-LD structured data for price, condition, and availability.
    Falls back to existing data if extraction fails.

    Args:
        page: A browser-use CDP Page instance.
        listing: Listing with listing_url set.

    Returns:
        A new Listing with enriched data, or the original if extraction fails.
    """
    if not listing.listing_url:
        return listing

    try:
        await navigate_and_wait(page, listing.listing_url, wait_ms=2000)

        # Extract Open Graph metadata
        og_data: dict = await page.evaluate(EXTRACT_OPEN_GRAPH_JS) or {}

        # Extract JSON-LD structured data
        ld_data: dict = await page.evaluate(EXTRACT_JSON_LD_JS) or {}

        # Extract full page text for description enrichment
        page_text = ""
        try:
            page_text, _ = await page._extract_clean_markdown()
        except Exception:
            pass

        # Build enriched fields
        title = og_data.get("title") or listing.title
        description = og_data.get("description") or listing.description or page_text[:500]

        # Merge images
        image_urls = list(listing.image_urls)
        og_image = og_data.get("image")
        if og_image and og_image not in image_urls:
            image_urls.insert(0, og_image)

        # Extract price from JSON-LD if we don't already have one
        price = listing.price
        if price is None:
            price_str = str(ld_data.get("price") or ld_data.get("lowPrice") or "")
            try:
                price = float(price_str.replace(",", ""))
            except (ValueError, TypeError):
                pass

        # Store raw structured data for debugging
        raw_data = dict(listing.raw_data)
        raw_data["og"] = og_data
        raw_data["ld"] = ld_data

        enriched = replace(
            listing,
            title=title,
            description=description,
            image_urls=image_urls,
            price=price,
            raw_data=raw_data,
        )

        log.debug(
            "Listing details extracted",
            external_id=listing.external_id,
            title=title[:50] if title else "",
            has_og=bool(og_data),
            has_ld=bool(ld_data),
        )
        return enriched

    except Exception as exc:
        log.warning(
            "Detail extraction failed, keeping existing data",
            external_id=listing.external_id,
            error=str(exc),
        )
        return listing
