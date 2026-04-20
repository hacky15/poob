"""Tests for EbayLookupTool - eBay sold price lookup via Tavily."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from poob.skills.models import PriceLookupResult


class TestExtractPricesFromText:
    """Tests for extracting prices from search result text."""

    def test_extract_prices_from_text(self):
        """Should extract dollar prices from plain text."""
        from poob.skills.ebay_lookup import extract_prices_from_text

        text = (
            "Sony PS5 Disc Edition Console sold for $350.00\n"
            "PS5 Bundle w/ Controller - $380.00\n"
            "PlayStation 5 Used - $320.00\n"
        )
        prices = extract_prices_from_text(text)

        assert len(prices) == 3
        assert 320.0 in prices
        assert 350.0 in prices
        assert 380.0 in prices

    def test_extract_prices_filters_outliers(self):
        """Should filter prices below $1 and above $50,000."""
        from poob.skills.ebay_lookup import extract_prices_from_text

        text = "Item sold for $0.50 another at $100.00 and $99,999.99"
        prices = extract_prices_from_text(text)

        assert prices == [100.0]

    def test_extract_prices_handles_commas(self):
        """Should parse comma-separated prices like $1,500.00."""
        from poob.skills.ebay_lookup import extract_prices_from_text

        text = "Sold for $1,500.00 and $2,300.50"
        prices = extract_prices_from_text(text)

        assert 1500.0 in prices
        assert 2300.50 in prices

    def test_extract_prices_empty_text(self):
        """Should return empty list for text with no prices."""
        from poob.skills.ebay_lookup import extract_prices_from_text

        assert extract_prices_from_text("No prices here") == []
        assert extract_prices_from_text("") == []


class TestSanitizeSearchQuery:
    """Tests for _sanitize_search_query — defense against Tavily 432 errors."""

    def test_strips_amazon_prefix(self):
        from poob.skills.ebay_lookup import _sanitize_search_query

        assert _sanitize_search_query("Amazon.com: TCL 85 Class TV") == "TCL 85 Class TV"

    def test_strips_walmart_prefix(self):
        from poob.skills.ebay_lookup import _sanitize_search_query

        assert _sanitize_search_query("Walmart.com: Crockpot 6qt") == "Crockpot 6qt"

    def test_strips_trailing_ellipsis(self):
        from poob.skills.ebay_lookup import _sanitize_search_query

        result = _sanitize_search_query("Some Product Name...")
        assert not result.endswith(".")

    def test_strips_quotes_and_ampersands(self):
        from poob.skills.ebay_lookup import _sanitize_search_query

        result = _sanitize_search_query('80\'s & 90\'s VTG Lot of 30 Pillsbury')
        assert '"' not in result
        assert "'" not in result
        assert "&" not in result
        assert "and" in result

    def test_truncates_long_names(self):
        from poob.skills.ebay_lookup import _sanitize_search_query

        long_name = "A " * 60  # 120 chars
        result = _sanitize_search_query(long_name)
        assert len(result) <= 80

    def test_returns_empty_for_only_special_chars(self):
        from poob.skills.ebay_lookup import _sanitize_search_query

        assert _sanitize_search_query("...") == ""

    def test_passthrough_clean_name(self):
        from poob.skills.ebay_lookup import _sanitize_search_query

        assert _sanitize_search_query("IKEA MALM desk") == "IKEA MALM desk"


class TestEbayPriceStats:
    """Tests for computing price statistics from eBay results."""

    def test_compute_stats_normal(self):
        """Should compute correct median, average, min, max."""
        from poob.skills.ebay_lookup import compute_price_stats

        prices = [300.0, 350.0, 380.0, 400.0, 320.0]
        stats = compute_price_stats(prices, query="PS5", source="ebay_sold")

        assert isinstance(stats, PriceLookupResult)
        assert stats.median_price == 350.0
        assert stats.average_price == pytest.approx(350.0, abs=1)
        assert stats.min_price == 300.0
        assert stats.max_price == 400.0
        assert stats.sample_count == 5
        assert stats.source == "ebay_sold"

    def test_compute_stats_single_price(self):
        """Should handle a single price point."""
        from poob.skills.ebay_lookup import compute_price_stats

        stats = compute_price_stats([250.0], query="test", source="ebay_sold")

        assert stats.median_price == 250.0
        assert stats.average_price == 250.0
        assert stats.sample_count == 1

    def test_compute_stats_empty(self):
        """Should return zero result for empty prices."""
        from poob.skills.ebay_lookup import compute_price_stats

        stats = compute_price_stats([], query="test", source="ebay_sold")

        assert stats.sample_count == 0
        assert stats.median_price == 0.0
        assert stats.confidence == 0.0

    def test_confidence_increases_with_samples(self):
        """More samples should yield higher confidence."""
        from poob.skills.ebay_lookup import compute_price_stats

        few = compute_price_stats([100.0, 200.0], query="q", source="s")
        many = compute_price_stats(
            [100.0, 150.0, 200.0, 180.0, 170.0, 190.0, 160.0, 175.0],
            query="q", source="s",
        )

        assert many.confidence > few.confidence


class TestEbayLookupTool:
    """Tests for the EbayLookupTool high-level interface."""

    async def test_tavily_lookup_success(self):
        """Should return price data when Tavily search succeeds."""
        from poob.skills.ebay_lookup import EbayLookupTool

        mock_search = AsyncMock()
        mock_search.search = AsyncMock(return_value=(
            "PS5 Disc Edition sold for $350.00\n"
            "PS5 Console Used - $320.00\n"
            "PlayStation 5 Bundle - $380.00\n"
        ))

        tool = EbayLookupTool(search_provider=mock_search)
        result = await tool.run("PS5 Disc Edition")

        assert isinstance(result, PriceLookupResult)
        assert result.sample_count == 3
        assert result.source == "ebay_sold"
        assert result.median_price == 350.0

    async def test_tavily_empty_results(self):
        """Should return empty result when search returns nothing."""
        from poob.skills.ebay_lookup import EbayLookupTool

        mock_search = AsyncMock()
        mock_search.search = AsyncMock(return_value="")

        tool = EbayLookupTool(search_provider=mock_search)
        result = await tool.run("PS5 Disc Edition")

        assert result.sample_count == 0
        assert result.confidence == 0.0

    async def test_tavily_error_returns_empty(self):
        """Should return empty result when search raises."""
        from poob.skills.ebay_lookup import EbayLookupTool

        mock_search = AsyncMock()
        mock_search.search = AsyncMock(side_effect=Exception("API error"))

        tool = EbayLookupTool(search_provider=mock_search)
        result = await tool.run("PS5 Disc Edition")

        assert result.sample_count == 0
        assert result.confidence == 0.0

    async def test_includes_ebay_site_filter_in_query(self):
        """Should include site:ebay.com in the search query string."""
        from poob.skills.ebay_lookup import EbayLookupTool

        mock_search = AsyncMock()
        mock_search.search = AsyncMock(return_value="$100.00")

        tool = EbayLookupTool(search_provider=mock_search)
        await tool.run("PS5")

        mock_search.search.assert_called_once()
        query = mock_search.search.call_args[0][0]
        assert "site:ebay.com" in query
        assert "PS5" in query

    async def test_includes_condition_in_query(self):
        """Should include condition in search query when provided."""
        from poob.skills.ebay_lookup import EbayLookupTool

        mock_search = AsyncMock()
        mock_search.search = AsyncMock(return_value="$200.00")

        tool = EbayLookupTool(search_provider=mock_search)
        await tool.run("iPhone 15 Pro", condition="used")

        query = mock_search.search.call_args[0][0]
        assert "used" in query
        assert "iPhone 15 Pro" in query
