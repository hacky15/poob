"""Tests for EbayLookupTool - eBay sold price lookup."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agentic_scraper.skills.models import PriceLookupResult


# Minimal eBay sold listings HTML fixture
EBAY_SOLD_HTML = """
<html><body>
<ul class="srp-results">
  <li class="s-item">
    <span class="s-item__title">Sony PS5 Disc Edition Console</span>
    <span class="s-item__price">$350.00</span>
  </li>
  <li class="s-item">
    <span class="s-item__title">PS5 Disc Edition Bundle w/ Controller</span>
    <span class="s-item__price">$380.00</span>
  </li>
  <li class="s-item">
    <span class="s-item__title">PlayStation 5 Disc Console - Used</span>
    <span class="s-item__price">$320.00</span>
  </li>
  <li class="s-item">
    <span class="s-item__title">PS5 Disc Edition White</span>
    <span class="s-item__price">$340.00</span>
  </li>
  <li class="s-item">
    <span class="s-item__title">PS5 Controller DualSense - White</span>
    <span class="s-item__price">$35.00</span>
  </li>
</ul>
</body></html>
"""


class TestEbayHtmlParser:
    """Tests for parsing eBay search results HTML."""

    def test_parse_prices_from_html(self):
        """Should extract prices from eBay sold listings HTML."""
        from agentic_scraper.skills.ebay_lookup import parse_ebay_sold_html

        items = parse_ebay_sold_html(EBAY_SOLD_HTML)

        assert len(items) >= 4
        titles = [item["title"] for item in items]
        prices = [item["price"] for item in items]
        assert any("PS5" in t for t in titles)
        assert all(isinstance(p, float) for p in prices)

    def test_parse_empty_html(self):
        """Should return empty list for pages with no results."""
        from agentic_scraper.skills.ebay_lookup import parse_ebay_sold_html

        items = parse_ebay_sold_html("<html><body>No results</body></html>")
        assert items == []

    def test_parse_handles_price_ranges(self):
        """Should handle price range formats like '$100.00 to $200.00'."""
        from agentic_scraper.skills.ebay_lookup import parse_ebay_sold_html

        html = """
        <ul class="srp-results">
          <li class="s-item">
            <span class="s-item__title">Test Item</span>
            <span class="s-item__price">$100.00 to $200.00</span>
          </li>
        </ul>
        """
        items = parse_ebay_sold_html(html)
        # Should take the first price in a range
        assert len(items) == 1
        assert items[0]["price"] == 100.0


class TestEbayPriceStats:
    """Tests for computing price statistics from eBay results."""

    def test_compute_stats_normal(self):
        """Should compute correct median, average, min, max."""
        from agentic_scraper.skills.ebay_lookup import compute_price_stats

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
        from agentic_scraper.skills.ebay_lookup import compute_price_stats

        stats = compute_price_stats([250.0], query="test", source="ebay_sold")

        assert stats.median_price == 250.0
        assert stats.average_price == 250.0
        assert stats.sample_count == 1

    def test_compute_stats_empty(self):
        """Should return zero result for empty prices."""
        from agentic_scraper.skills.ebay_lookup import compute_price_stats

        stats = compute_price_stats([], query="test", source="ebay_sold")

        assert stats.sample_count == 0
        assert stats.median_price == 0.0
        assert stats.confidence == 0.0

    def test_confidence_increases_with_samples(self):
        """More samples should yield higher confidence."""
        from agentic_scraper.skills.ebay_lookup import compute_price_stats

        few = compute_price_stats([100.0, 200.0], query="q", source="s")
        many = compute_price_stats(
            [100.0, 150.0, 200.0, 180.0, 170.0, 190.0, 160.0, 175.0],
            query="q", source="s",
        )

        assert many.confidence > few.confidence


class TestEbayLookupTool:
    """Tests for the EbayLookupTool high-level interface."""

    async def test_http_lookup_success(self):
        """Should return price data when HTTP scraping succeeds."""
        from agentic_scraper.skills.ebay_lookup import EbayLookupTool

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = EBAY_SOLD_HTML

        with patch("agentic_scraper.skills.ebay_lookup.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            tool = EbayLookupTool(timeout_seconds=10)
            result = await tool.run("PS5 Disc Edition")

        assert isinstance(result, PriceLookupResult)
        assert result.sample_count > 0
        assert result.source == "ebay_sold"

    async def test_http_failure_returns_empty(self):
        """Should return empty result when HTTP scraping fails."""
        from agentic_scraper.skills.ebay_lookup import EbayLookupTool

        with patch("agentic_scraper.skills.ebay_lookup.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(side_effect=Exception("Connection failed"))
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            tool = EbayLookupTool(timeout_seconds=5)
            result = await tool.run("PS5 Disc Edition")

        assert result.sample_count == 0
        assert result.confidence == 0.0

    async def test_builds_correct_ebay_url(self):
        """Should construct eBay sold items search URL correctly."""
        from agentic_scraper.skills.ebay_lookup import EbayLookupTool

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = "<html></html>"

        with patch("agentic_scraper.skills.ebay_lookup.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            tool = EbayLookupTool(timeout_seconds=10)
            await tool.run("PS5 Disc Edition")

            call_url = mock_client.get.call_args[0][0]
            assert "ebay.com" in call_url
            assert "LH_Sold=1" in call_url
            assert "LH_Complete=1" in call_url
            assert "PS5" in call_url

    async def test_filters_condition_in_query(self):
        """Should include condition in search when provided."""
        from agentic_scraper.skills.ebay_lookup import EbayLookupTool

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = "<html></html>"

        with patch("agentic_scraper.skills.ebay_lookup.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            tool = EbayLookupTool(timeout_seconds=10)
            await tool.run("iPhone 15 Pro", condition="used")

            call_url = mock_client.get.call_args[0][0]
            assert "iPhone" in call_url
