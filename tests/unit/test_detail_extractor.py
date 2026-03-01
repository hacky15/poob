"""Tests for detail_extractor - enriching listings with OG + JSON-LD data."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

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


@pytest.fixture
def mock_page():
    page = AsyncMock()
    page.evaluate = AsyncMock(return_value={})
    page._extract_clean_markdown = AsyncMock(return_value=("Some page text", {}))
    return page


# --- Basic extraction ---


class TestExtractListingDetails:
    @pytest.mark.asyncio
    async def test_enriches_title_from_og(self, mock_page, base_listing):
        """OG title should override the surface-scraped title."""
        mock_page.evaluate = AsyncMock(
            side_effect=[
                {"title": "PlayStation 5 Console Bundle", "description": "Great deal"},
                {},  # JSON-LD
            ]
        )

        result = await extract_listing_details(mock_page, base_listing)

        assert result.title == "PlayStation 5 Console Bundle"

    @pytest.mark.asyncio
    async def test_enriches_description_from_og(self, mock_page, base_listing):
        mock_page.evaluate = AsyncMock(
            side_effect=[
                {"description": "Barely used PS5 with 2 controllers"},
                {},
            ]
        )

        result = await extract_listing_details(mock_page, base_listing)

        assert result.description == "Barely used PS5 with 2 controllers"

    @pytest.mark.asyncio
    async def test_enriches_image_from_og(self, mock_page, base_listing):
        og_image = "https://scontent.xx.fbcdn.net/og_image.jpg"
        mock_page.evaluate = AsyncMock(
            side_effect=[
                {"image": og_image},
                {},
            ]
        )

        result = await extract_listing_details(mock_page, base_listing)

        # OG image should be prepended
        assert result.image_urls[0] == og_image
        # Original image still present
        assert "https://scontent.xx.fbcdn.net/original.jpg" in result.image_urls

    @pytest.mark.asyncio
    async def test_no_duplicate_og_image(self, mock_page, base_listing):
        """If OG image is already in image_urls, don't add it again."""
        existing_img = base_listing.image_urls[0]
        mock_page.evaluate = AsyncMock(
            side_effect=[
                {"image": existing_img},
                {},
            ]
        )

        result = await extract_listing_details(mock_page, base_listing)

        assert result.image_urls.count(existing_img) == 1

    @pytest.mark.asyncio
    async def test_extracts_price_from_json_ld(self, mock_page, base_listing):
        """JSON-LD price should be used when listing has no price."""
        no_price_listing = Listing(
            id="listing-2",
            site="facebook_marketplace",
            external_id="222",
            title="Table",
            price=None,
            listing_url="https://www.facebook.com/marketplace/item/222",
        )
        mock_page.evaluate = AsyncMock(
            side_effect=[
                {},  # OG
                {"price": "150.00", "@type": "Product"},  # JSON-LD
            ]
        )

        result = await extract_listing_details(mock_page, no_price_listing)

        assert result.price == 150.0

    @pytest.mark.asyncio
    async def test_keeps_existing_price_over_json_ld(self, mock_page, base_listing):
        """If listing already has a price, don't override with JSON-LD."""
        mock_page.evaluate = AsyncMock(
            side_effect=[
                {},
                {"price": "999.00"},
            ]
        )

        result = await extract_listing_details(mock_page, base_listing)

        assert result.price == 300.0  # Original price preserved

    @pytest.mark.asyncio
    async def test_stores_raw_data(self, mock_page, base_listing):
        """OG and JSON-LD data should be saved in raw_data for debugging."""
        og = {"title": "PS5", "description": "Good"}
        ld = {"price": "300", "@type": "Product"}
        mock_page.evaluate = AsyncMock(side_effect=[og, ld])

        result = await extract_listing_details(mock_page, base_listing)

        assert result.raw_data["og"] == og
        assert result.raw_data["ld"] == ld


# --- Fallback behavior ---


class TestDetailExtractionFallbacks:
    @pytest.mark.asyncio
    async def test_returns_original_on_no_url(self, mock_page):
        """Listing without a URL should be returned unchanged."""
        no_url_listing = Listing(
            id="listing-x",
            site="facebook_marketplace",
            external_id="999",
            title="No URL Item",
        )

        result = await extract_listing_details(mock_page, no_url_listing)

        assert result is no_url_listing

    @pytest.mark.asyncio
    async def test_returns_original_on_navigation_error(self, mock_page, base_listing):
        """Navigation failure should return the original listing."""
        with patch(
            "agentic_scraper.sites.facebook.detail_extractor.navigate_and_wait",
            new_callable=AsyncMock,
            side_effect=Exception("Timeout"),
        ):
            result = await extract_listing_details(mock_page, base_listing)

        assert result is base_listing

    @pytest.mark.asyncio
    async def test_keeps_title_when_og_empty(self, mock_page, base_listing):
        """Empty OG data should not blank out existing title."""
        mock_page.evaluate = AsyncMock(side_effect=[{}, {}])

        result = await extract_listing_details(mock_page, base_listing)

        assert result.title == "PS5 Console"

    @pytest.mark.asyncio
    async def test_handles_invalid_json_ld_price(self, mock_page, base_listing):
        """Non-numeric JSON-LD price should be ignored."""
        no_price_listing = Listing(
            id="listing-np",
            site="facebook_marketplace",
            external_id="444",
            title="Stuff",
            price=None,
            listing_url="https://www.facebook.com/marketplace/item/444",
        )
        mock_page.evaluate = AsyncMock(
            side_effect=[
                {},
                {"price": "contact for price"},
            ]
        )

        result = await extract_listing_details(mock_page, no_price_listing)

        assert result.price is None

    @pytest.mark.asyncio
    async def test_handles_markdown_extraction_failure(self, mock_page, base_listing):
        """If _extract_clean_markdown fails, should still work."""
        mock_page.evaluate = AsyncMock(side_effect=[{}, {}])
        mock_page._extract_clean_markdown = AsyncMock(side_effect=Exception("No method"))

        result = await extract_listing_details(mock_page, base_listing)

        # Should still succeed (description falls back to empty or existing)
        assert result.title == "PS5 Console"


# --- Immutability ---


class TestDetailExtractionImmutability:
    @pytest.mark.asyncio
    async def test_does_not_mutate_original_listing(self, mock_page, base_listing):
        """extract_listing_details should return a new Listing, not mutate the original."""
        mock_page.evaluate = AsyncMock(
            side_effect=[
                {"title": "New Title"},
                {},
            ]
        )

        result = await extract_listing_details(mock_page, base_listing)

        assert result is not base_listing
        assert base_listing.title == "PS5 Console"
        assert result.title == "New Title"

    @pytest.mark.asyncio
    async def test_does_not_mutate_original_image_urls(self, mock_page, base_listing):
        """Original listing's image_urls list should not be modified."""
        original_images = list(base_listing.image_urls)
        mock_page.evaluate = AsyncMock(
            side_effect=[
                {"image": "https://new-image.jpg"},
                {},
            ]
        )

        await extract_listing_details(mock_page, base_listing)

        assert base_listing.image_urls == original_images
