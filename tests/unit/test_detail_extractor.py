"""Tests for detail_extractor - enriching listings with data-sjs + DOM data.

The extractor calls page.evaluate() twice:
  1. EXTRACT_DATA_SJS_JS → returns list[str] (JSON payloads from data-sjs)
  2. EXTRACT_DOM_DETAIL_JS → returns dict (DOM structural extraction)
Then optionally page._extract_clean_markdown() for freshness/seller.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from agentic_scraper.sites.facebook.detail_extractor import extract_listing_details
from agentic_scraper.storage.models import Listing


@pytest.fixture
def base_listing() -> Listing:
    return Listing(
        id="listing-1",
        site="facebook_marketplace",
        external_id="111",
        title="PS5 Console",
        price=300.0,
        listing_url="https://www.facebook.com/marketplace/item/111",
        image_urls=["https://scontent.xx.fbcdn.net/original.jpg"],
    )


def _sjs_payload(**fields) -> str:
    """Build a fake data-sjs JSON string with marketplace fields."""
    import json

    data = {}
    if "title" in fields:
        data["marketplace_listing_title"] = fields["title"]
    if "description" in fields:
        data["redacted_description"] = {"text": fields["description"]}
    if "price" in fields:
        data["listing_price"] = {"amount": str(fields["price"])}
    if "creation_time" in fields:
        data["creation_time"] = fields["creation_time"]
    if "condition" in fields:
        data["marketplace_listing_condition_type"] = fields["condition"]
    if "seller_name" in fields:
        data["marketplace_listing_seller"] = {"name": fields["seller_name"]}
    if "location" in fields:
        data["location_text"] = fields["location"]
    return json.dumps(data)


def _make_evaluate(sjs_result=None, dom_result=None):
    """Create a mock evaluate function that returns the right data based on JS content.

    The detail extractor calls page.evaluate() multiple times:
    - With EXTRACT_DATA_SJS_JS (contains 'data-sjs') → returns sjs_result
    - With EXTRACT_DOM_DETAIL_JS (contains 'role="main"') → returns dom_result
    - With diagnostic JS or other → returns None
    """
    async def _evaluate(js_code, *args, **kwargs):
        js_str = str(js_code) if js_code else ""
        if "data-sjs" in js_str or "marketplace_listing_title" in js_str:
            return sjs_result if sjs_result is not None else []
        if 'role="main"' in js_str or "result.title" in js_str:
            return dom_result if dom_result is not None else {}
        # Diagnostic or other JS
        return None
    return _evaluate


@pytest.fixture
def mock_page():
    page = AsyncMock()
    # Default: empty results from both tiers
    page.evaluate = AsyncMock(side_effect=_make_evaluate())
    page._extract_clean_markdown = AsyncMock(return_value=("Some page text", {}))
    return page


# --- data-sjs extraction (Tier 1) ---


class TestDataSjsExtraction:
    @pytest.mark.asyncio
    async def test_enriches_title_from_data_sjs(self, mock_page, base_listing):
        """data-sjs title should override the surface-scraped title."""
        mock_page.evaluate = AsyncMock(side_effect=_make_evaluate(
            sjs_result=[_sjs_payload(title="PlayStation 5 Console Bundle")],
        ))
        result = await extract_listing_details(mock_page, base_listing)
        assert result.title == "PlayStation 5 Console Bundle"

    @pytest.mark.asyncio
    async def test_enriches_description_from_data_sjs(self, mock_page, base_listing):
        mock_page.evaluate = AsyncMock(side_effect=_make_evaluate(
            sjs_result=[_sjs_payload(description="Barely used PS5 with 2 controllers")],
        ))
        result = await extract_listing_details(mock_page, base_listing)
        assert result.description == "Barely used PS5 with 2 controllers"

    @pytest.mark.asyncio
    async def test_enriches_price_from_data_sjs(self, mock_page):
        no_price = Listing(
            id="listing-2", site="facebook_marketplace",
            external_id="222", title="Table", price=None,
            listing_url="https://www.facebook.com/marketplace/item/222",
        )
        mock_page.evaluate = AsyncMock(side_effect=_make_evaluate(
            sjs_result=[_sjs_payload(price=150.0)],
        ))
        result = await extract_listing_details(mock_page, no_price)
        assert result.price == 150.0

    @pytest.mark.asyncio
    async def test_keeps_existing_price_over_data_sjs(self, mock_page, base_listing):
        """If listing already has a price, don't override with data-sjs."""
        mock_page.evaluate = AsyncMock(side_effect=_make_evaluate(
            sjs_result=[_sjs_payload(price=999.0)],
        ))
        result = await extract_listing_details(mock_page, base_listing)
        assert result.price == 300.0  # Original price preserved

    @pytest.mark.asyncio
    async def test_enriches_posted_at_from_creation_time(self, mock_page, base_listing):
        mock_page.evaluate = AsyncMock(side_effect=_make_evaluate(
            sjs_result=[_sjs_payload(creation_time=1710000000)],
        ))
        result = await extract_listing_details(mock_page, base_listing)
        assert result.posted_at is not None
        assert isinstance(result.posted_at, datetime)

    @pytest.mark.asyncio
    async def test_enriches_condition(self, mock_page, base_listing):
        mock_page.evaluate = AsyncMock(side_effect=_make_evaluate(
            sjs_result=[_sjs_payload(condition="USED_GOOD")],
        ))
        result = await extract_listing_details(mock_page, base_listing)
        assert result.raw_data.get("condition") == "USED_GOOD"

    @pytest.mark.asyncio
    async def test_enriches_seller_name(self, mock_page, base_listing):
        mock_page.evaluate = AsyncMock(side_effect=_make_evaluate(
            sjs_result=[_sjs_payload(seller_name="John Doe")],
        ))
        result = await extract_listing_details(mock_page, base_listing)
        assert result.seller_name == "John Doe"


# --- DOM extraction (Tier 2 fallback) ---


class TestDomExtraction:
    @pytest.mark.asyncio
    async def test_dom_title_fills_when_sjs_empty(self, mock_page, base_listing):
        mock_page.evaluate = AsyncMock(side_effect=_make_evaluate(
            dom_result={"title": "PS5 Digital Edition"},
        ))
        result = await extract_listing_details(mock_page, base_listing)
        assert result.title == "PS5 Digital Edition"

    @pytest.mark.asyncio
    async def test_dom_description_fills_when_sjs_empty(self, mock_page, base_listing):
        mock_page.evaluate = AsyncMock(side_effect=_make_evaluate(
            dom_result={"description": "Great condition, includes cables"},
        ))
        result = await extract_listing_details(mock_page, base_listing)
        assert result.description == "Great condition, includes cables"

    @pytest.mark.asyncio
    async def test_dom_images_fill_when_sjs_empty(self, mock_page, base_listing):
        mock_page.evaluate = AsyncMock(side_effect=_make_evaluate(
            dom_result={"image_urls": ["https://scontent.xx.fbcdn.net/new.jpg"]},
        ))
        result = await extract_listing_details(mock_page, base_listing)
        assert "https://scontent.xx.fbcdn.net/new.jpg" in result.image_urls
        # Original image still present
        assert "https://scontent.xx.fbcdn.net/original.jpg" in result.image_urls


# --- Fallback behavior ---


class TestDetailExtractionFallbacks:
    @pytest.mark.asyncio
    async def test_returns_original_on_no_url(self, mock_page):
        no_url = Listing(
            id="listing-x", site="facebook_marketplace",
            external_id="999", title="No URL Item",
        )
        result = await extract_listing_details(mock_page, no_url)
        assert result is no_url

    @pytest.mark.asyncio
    async def test_returns_original_on_navigation_error(self, mock_page, base_listing):
        with patch(
            "agentic_scraper.sites.facebook.detail_extractor.navigate_and_wait",
            new_callable=AsyncMock,
            side_effect=Exception("Timeout"),
        ):
            result = await extract_listing_details(mock_page, base_listing)
        assert result is base_listing

    @pytest.mark.asyncio
    async def test_keeps_title_when_both_tiers_empty(self, mock_page, base_listing):
        mock_page.evaluate = AsyncMock(side_effect=_make_evaluate())
        result = await extract_listing_details(mock_page, base_listing)
        assert result.title == "PS5 Console"

    @pytest.mark.asyncio
    async def test_handles_markdown_extraction_failure(self, mock_page, base_listing):
        mock_page.evaluate = AsyncMock(side_effect=_make_evaluate())
        mock_page._extract_clean_markdown = AsyncMock(side_effect=Exception("No method"))
        result = await extract_listing_details(mock_page, base_listing)
        assert result.title == "PS5 Console"


# --- Immutability ---


class TestDetailExtractionImmutability:
    @pytest.mark.asyncio
    async def test_does_not_mutate_original_listing(self, mock_page, base_listing):
        mock_page.evaluate = AsyncMock(side_effect=_make_evaluate(
            sjs_result=[_sjs_payload(title="New Title")],
        ))
        result = await extract_listing_details(mock_page, base_listing)
        assert result is not base_listing
        assert base_listing.title == "PS5 Console"
        assert result.title == "New Title"

    @pytest.mark.asyncio
    async def test_does_not_mutate_original_image_urls(self, mock_page, base_listing):
        original_images = list(base_listing.image_urls)
        mock_page.evaluate = AsyncMock(side_effect=_make_evaluate(
            dom_result={"image_urls": ["https://new-image.jpg"]},
        ))
        await extract_listing_details(mock_page, base_listing)
        assert base_listing.image_urls == original_images
