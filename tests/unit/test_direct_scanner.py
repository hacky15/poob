"""Unit tests for the Facebook Marketplace DirectScanner."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agentic_scraper.sites.base import ScanQuery, ScanResult
from agentic_scraper.sites.facebook.direct_scanner import DirectScanner


@pytest.fixture
def scanner():
    """DirectScanner with default settings."""
    return DirectScanner(max_listings=20)


@pytest.fixture
def mock_page():
    """Mock CDP Page."""
    page = AsyncMock()
    page.goto = AsyncMock()
    page.evaluate = AsyncMock(return_value="[]")
    page.screenshot = AsyncMock(return_value="base64data")
    page._extract_clean_markdown = AsyncMock(return_value=("# Content", {}))
    return page


@pytest.fixture
def mock_browser(mock_page):
    """Mock BrowserManager with get_page()."""
    browser = AsyncMock()
    browser.get_page = AsyncMock(return_value=mock_page)
    return browser


@pytest.fixture
def sample_query():
    """Standard test query."""
    return ScanQuery(keywords="PS5", max_price=300.0)


@pytest.fixture
def sample_js_result():
    """Sample JS extraction result as list (before JSON serialization)."""
    return [
        {
            "title": "PS5 Disc Edition",
            "price": 250.0,
            "location": "Portland, OR",
            "listing_url": "https://www.facebook.com/marketplace/item/12345",
            "external_id": "12345",
            "image_url": "https://scontent.fbcdn.net/ps5.jpg",
        },
        {
            "title": "PS5 Digital",
            "price": 200.0,
            "location": "Seattle, WA",
            "listing_url": "https://www.facebook.com/marketplace/item/67890",
            "external_id": "67890",
            "image_url": "",
        },
    ]


class TestBuildSearchUrl:
    """Tests for DirectScanner._build_search_url()."""

    def test_basic_keywords(self, scanner):
        """Should include keywords in the URL."""
        query = ScanQuery(keywords="PS5")
        url = scanner._build_search_url(query)
        assert "query=PS5" in url
        assert "sortBy=creation_time_descend" in url

    def test_keywords_with_spaces(self, scanner):
        """Should URL-encode spaces in keywords."""
        query = ScanQuery(keywords="coffee table")
        url = scanner._build_search_url(query)
        assert "query=coffee+table" in url

    def test_with_max_price(self, scanner):
        """Should append maxPrice when provided."""
        query = ScanQuery(keywords="PS5", max_price=300.0)
        url = scanner._build_search_url(query)
        assert "maxPrice=300" in url

    def test_without_max_price(self, scanner):
        """Should not include maxPrice when None."""
        query = ScanQuery(keywords="PS5")
        url = scanner._build_search_url(query)
        assert "maxPrice" not in url

    def test_url_starts_with_marketplace(self, scanner):
        """Should use the Facebook Marketplace search endpoint."""
        query = ScanQuery(keywords="test")
        url = scanner._build_search_url(query)
        assert url.startswith("https://www.facebook.com/marketplace/search/")

    def test_category_does_not_change_url(self, scanner):
        """Category should NOT change the URL — filtering is post-scrape."""
        query = ScanQuery(keywords="coffee table", category="furniture")
        url = scanner._build_search_url(query)
        assert "/marketplace/search/" in url
        assert "query=coffee+table" in url
        assert "category" not in url.lower()

    def test_url_with_category_still_has_max_price(self, scanner):
        """Category queries should still include maxPrice."""
        query = ScanQuery(keywords="table", max_price=100.0, category="furniture")
        url = scanner._build_search_url(query)
        assert "/marketplace/search/" in url
        assert "maxPrice=100" in url

    def test_empty_keywords_builds_browse_url(self, scanner):
        """Empty keywords should browse latest listings, not search."""
        query = ScanQuery(keywords="")
        url = scanner._build_search_url(query)
        assert "/marketplace/" in url
        assert "/marketplace/search/" not in url
        assert "sortBy=creation_time_descend" in url
        assert "query=" not in url

    def test_whitespace_keywords_builds_browse_url(self, scanner):
        """Whitespace-only keywords should also use browse URL."""
        query = ScanQuery(keywords="   ")
        url = scanner._build_search_url(query)
        assert "/marketplace/search/" not in url
        assert "sortBy=creation_time_descend" in url


class TestDirectScan:
    """Tests for DirectScanner.scan()."""

    async def test_scan_calls_navigate(self, scanner, mock_browser, mock_page, sample_query):
        """Should navigate to the search URL."""
        with patch(
            "agentic_scraper.sites.facebook.direct_scanner.navigate_and_wait",
            new_callable=AsyncMock,
        ):
            await scanner.scan(sample_query, mock_browser)
        # get_page should have been called
        mock_browser.get_page.assert_called_once()

    async def test_scan_returns_scan_result(
        self, scanner, mock_browser, mock_page, sample_query, sample_js_result
    ):
        """Should return a ScanResult with parsed listings."""
        mock_page.evaluate = AsyncMock(return_value=sample_js_result)

        with patch(
            "agentic_scraper.sites.facebook.direct_scanner.navigate_and_wait",
            new_callable=AsyncMock,
        ), patch(
            "agentic_scraper.sites.facebook.direct_scanner.apply_scroll_pattern",
            new_callable=AsyncMock,
        ):
            result = await scanner.scan(sample_query, mock_browser)

        assert isinstance(result, ScanResult)
        assert len(result.listings) == 2
        assert result.listings[0].title == "PS5 Disc Edition"
        assert result.listings[0].price == 250.0
        assert result.listings[0].site == "facebook_marketplace"

    async def test_scan_extracts_via_js_first(
        self, scanner, mock_browser, mock_page, sample_query, sample_js_result
    ):
        """JS extraction should be attempted before LLM fallback."""
        mock_page.evaluate = AsyncMock(return_value=sample_js_result)

        with patch(
            "agentic_scraper.sites.facebook.direct_scanner.navigate_and_wait",
            new_callable=AsyncMock,
        ), patch(
            "agentic_scraper.sites.facebook.direct_scanner.apply_scroll_pattern",
            new_callable=AsyncMock,
        ):
            result = await scanner.scan(sample_query, mock_browser)

        assert len(result.listings) == 2
        # LLM should NOT have been called
        mock_page._extract_clean_markdown.assert_not_called()

    async def test_scan_falls_back_to_llm_on_empty_js(
        self, scanner, mock_browser, mock_page, sample_query
    ):
        """Should use LLM fallback when JS extraction returns too few results."""
        # JS returns empty
        mock_page.evaluate = AsyncMock(return_value=[])
        # LLM fallback returns markdown
        mock_page._extract_clean_markdown = AsyncMock(
            return_value=("Some marketplace content", {})
        )

        mock_llm = MagicMock()
        llm_response = json.dumps({"listings": [
            {"title": "PS5", "price": 250.0, "external_id": "111"}
        ]})
        mock_llm.ainvoke = AsyncMock(
            return_value=MagicMock(content=llm_response)
        )

        with patch(
            "agentic_scraper.sites.facebook.direct_scanner.navigate_and_wait",
            new_callable=AsyncMock,
        ), patch(
            "agentic_scraper.sites.facebook.direct_scanner.apply_scroll_pattern",
            new_callable=AsyncMock,
        ):
            result = await scanner.scan(sample_query, mock_browser, llm=mock_llm)

        assert len(result.listings) == 1
        assert result.listings[0].title == "PS5"
        mock_page._extract_clean_markdown.assert_called_once()

    async def test_scan_returns_empty_on_all_failures(
        self, scanner, mock_browser, mock_page, sample_query
    ):
        """Should return empty ScanResult with errors when both paths fail."""
        mock_page.evaluate = AsyncMock(return_value=[])
        mock_page._extract_clean_markdown = AsyncMock(return_value=("", {}))

        with patch(
            "agentic_scraper.sites.facebook.direct_scanner.navigate_and_wait",
            new_callable=AsyncMock,
        ), patch(
            "agentic_scraper.sites.facebook.direct_scanner.apply_scroll_pattern",
            new_callable=AsyncMock,
        ):
            result = await scanner.scan(sample_query, mock_browser)

        assert isinstance(result, ScanResult)
        assert len(result.listings) == 0

    async def test_scan_handles_navigation_error(
        self, scanner, mock_browser, mock_page, sample_query
    ):
        """Should gracefully handle navigation errors."""
        mock_browser.get_page = AsyncMock(
            side_effect=RuntimeError("Browser not started")
        )

        result = await scanner.scan(sample_query, mock_browser)

        assert isinstance(result, ScanResult)
        assert len(result.listings) == 0
        assert len(result.errors) > 0
        assert "Browser not started" in result.errors[0]

    async def test_scan_handles_js_evaluation_error(
        self, scanner, mock_browser, mock_page, sample_query
    ):
        """Should handle JS evaluation errors and continue."""
        mock_page.evaluate = AsyncMock(
            side_effect=RuntimeError("JS failed")
        )
        mock_page._extract_clean_markdown = AsyncMock(return_value=("", {}))

        with patch(
            "agentic_scraper.sites.facebook.direct_scanner.navigate_and_wait",
            new_callable=AsyncMock,
        ), patch(
            "agentic_scraper.sites.facebook.direct_scanner.apply_scroll_pattern",
            new_callable=AsyncMock,
        ):
            result = await scanner.scan(sample_query, mock_browser)

        assert isinstance(result, ScanResult)
        # Should not crash, may have errors

    async def test_scan_applies_stealth_scroll(
        self, scanner, mock_browser, mock_page, sample_query, sample_js_result
    ):
        """Should execute stealth scrolling to load more listings."""
        mock_page.evaluate = AsyncMock(return_value=sample_js_result)

        with patch(
            "agentic_scraper.sites.facebook.direct_scanner.navigate_and_wait",
            new_callable=AsyncMock,
        ), patch(
            "agentic_scraper.sites.facebook.direct_scanner.apply_scroll_pattern",
            new_callable=AsyncMock,
        ) as mock_scroll:
            await scanner.scan(sample_query, mock_browser)

        mock_scroll.assert_called_once()

    async def test_scan_reuses_existing_parser(
        self, scanner, mock_browser, mock_page, sample_query, sample_js_result
    ):
        """Should use parse_listings() for consistent output format."""
        mock_page.evaluate = AsyncMock(return_value=sample_js_result)

        with patch(
            "agentic_scraper.sites.facebook.direct_scanner.navigate_and_wait",
            new_callable=AsyncMock,
        ), patch(
            "agentic_scraper.sites.facebook.direct_scanner.apply_scroll_pattern",
            new_callable=AsyncMock,
        ), patch(
            "agentic_scraper.sites.facebook.direct_scanner.parse_listings",
            wraps=__import__(
                "agentic_scraper.sites.facebook.parser", fromlist=["parse_listings"]
            ).parse_listings,
        ) as mock_parse:
            await scanner.scan(sample_query, mock_browser)

        mock_parse.assert_called_once()

    async def test_scan_sets_site_name(
        self, scanner, mock_browser, mock_page, sample_query, sample_js_result
    ):
        """All listings should have site set to facebook_marketplace."""
        mock_page.evaluate = AsyncMock(return_value=sample_js_result)

        with patch(
            "agentic_scraper.sites.facebook.direct_scanner.navigate_and_wait",
            new_callable=AsyncMock,
        ), patch(
            "agentic_scraper.sites.facebook.direct_scanner.apply_scroll_pattern",
            new_callable=AsyncMock,
        ):
            result = await scanner.scan(sample_query, mock_browser)

        for listing in result.listings:
            assert listing.site == "facebook_marketplace"

    async def test_scan_filters_irrelevant_by_category(
        self, scanner, mock_browser, mock_page
    ):
        """Listings that don't match the query category should be filtered out."""
        # Mix of furniture and books
        js_data = [
            {
                "title": "Coffee Table - Solid Oak",
                "price": 80.0,
                "external_id": "1",
                "listing_url": "https://www.facebook.com/marketplace/item/1",
            },
            {
                "title": "Coffee Table Book - Art Edition",
                "price": 15.0,
                "external_id": "2",
                "listing_url": "https://www.facebook.com/marketplace/item/2",
            },
            {
                "title": "Modern Glass Coffee Table",
                "price": 95.0,
                "external_id": "3",
                "listing_url": "https://www.facebook.com/marketplace/item/3",
            },
        ]
        mock_page.evaluate = AsyncMock(return_value=js_data)

        query = ScanQuery(keywords="coffee table", max_price=100.0, category="furniture")

        with patch(
            "agentic_scraper.sites.facebook.direct_scanner.navigate_and_wait",
            new_callable=AsyncMock,
        ), patch(
            "agentic_scraper.sites.facebook.direct_scanner.apply_scroll_pattern",
            new_callable=AsyncMock,
        ):
            result = await scanner.scan(query, mock_browser)

        # Book should be filtered out
        assert len(result.listings) == 2
        titles = [l.title for l in result.listings]
        assert "Coffee Table - Solid Oak" in titles
        assert "Modern Glass Coffee Table" in titles
        assert "Coffee Table Book - Art Edition" not in titles

    async def test_scan_infers_category_from_keywords(
        self, scanner, mock_browser, mock_page
    ):
        """Even without explicit category, inferred category should filter books."""
        js_data = [
            {
                "title": "Coffee Table - Solid Oak",
                "price": 80.0,
                "external_id": "1",
                "listing_url": "https://www.facebook.com/marketplace/item/1",
            },
            {
                "title": "Coffee Table Book - Art Edition",
                "price": 15.0,
                "external_id": "2",
                "listing_url": "https://www.facebook.com/marketplace/item/2",
            },
        ]
        mock_page.evaluate = AsyncMock(return_value=js_data)

        query = ScanQuery(keywords="coffee table", max_price=100.0)  # No explicit category

        with patch(
            "agentic_scraper.sites.facebook.direct_scanner.navigate_and_wait",
            new_callable=AsyncMock,
        ), patch(
            "agentic_scraper.sites.facebook.direct_scanner.apply_scroll_pattern",
            new_callable=AsyncMock,
        ):
            result = await scanner.scan(query, mock_browser)

        # Book should be filtered out via inferred "furniture" category
        assert len(result.listings) == 1
        assert result.listings[0].title == "Coffee Table - Solid Oak"

    async def test_scan_no_filter_for_unknown_keywords(
        self, scanner, mock_browser, mock_page
    ):
        """Keywords with no category mapping should not filter anything."""
        js_data = [
            {
                "title": "Random Item A",
                "price": 10.0,
                "external_id": "1",
                "listing_url": "https://www.facebook.com/marketplace/item/1",
            },
            {
                "title": "Random Item B",
                "price": 20.0,
                "external_id": "2",
                "listing_url": "https://www.facebook.com/marketplace/item/2",
            },
        ]
        mock_page.evaluate = AsyncMock(return_value=js_data)

        query = ScanQuery(keywords="random stuff", max_price=100.0)  # No category match

        with patch(
            "agentic_scraper.sites.facebook.direct_scanner.navigate_and_wait",
            new_callable=AsyncMock,
        ), patch(
            "agentic_scraper.sites.facebook.direct_scanner.apply_scroll_pattern",
            new_callable=AsyncMock,
        ):
            result = await scanner.scan(query, mock_browser)

        # Both should be present - no filtering for unknown keywords
        assert len(result.listings) == 2

    async def test_scan_browse_mode_returns_all_listings(
        self, scanner, mock_browser, mock_page
    ):
        """Browse mode (empty keywords) should return all listings unfiltered."""
        js_data = [
            {
                "title": "Coffee Table Book",
                "price": 15.0,
                "external_id": "1",
                "listing_url": "https://www.facebook.com/marketplace/item/1",
            },
            {
                "title": "Vintage Lamp",
                "price": 45.0,
                "external_id": "2",
                "listing_url": "https://www.facebook.com/marketplace/item/2",
            },
            {
                "title": "PS5 Controller",
                "price": 40.0,
                "external_id": "3",
                "listing_url": "https://www.facebook.com/marketplace/item/3",
            },
        ]
        mock_page.evaluate = AsyncMock(return_value=js_data)

        query = ScanQuery(keywords="")  # Browse mode

        with patch(
            "agentic_scraper.sites.facebook.direct_scanner.navigate_and_wait",
            new_callable=AsyncMock,
        ), patch(
            "agentic_scraper.sites.facebook.direct_scanner.apply_scroll_pattern",
            new_callable=AsyncMock,
        ):
            result = await scanner.scan(query, mock_browser)

        # All 3 should be present - no category filtering in browse mode
        assert len(result.listings) == 3
