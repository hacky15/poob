"""Integration tests for the direct CDP scan flow."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from agentic_scraper.sites.base import ScanQuery, ScanResult


@pytest.fixture
def sample_js_listings():
    """Listings as returned by JS extraction."""
    return [
        {
            "title": "PS5 Disc Edition",
            "price": 250.0,
            "location": "Portland, OR",
            "listing_url": "https://www.facebook.com/marketplace/item/111",
            "external_id": "111",
            "image_url": "https://scontent.fbcdn.net/ps5.jpg",
        },
        {
            "title": "Xbox Series X",
            "price": 300.0,
            "location": "Seattle, WA",
            "listing_url": "https://www.facebook.com/marketplace/item/222",
            "external_id": "222",
            "image_url": "",
        },
        {
            "title": "Nintendo Switch OLED",
            "price": 200.0,
            "location": "Beaverton, OR",
            "listing_url": "https://www.facebook.com/marketplace/item/333",
            "external_id": "333",
            "image_url": "",
        },
    ]


class TestAdapterDirectMode:
    """Tests for adapter dispatching to direct scanner."""

    async def test_direct_mode_uses_direct_scanner(
        self, mock_page, mock_browser_manager, sample_js_listings
    ):
        """With scan_mode='direct', adapter should use DirectScanner."""
        from agentic_scraper.sites.facebook.adapter import FacebookMarketplaceAdapter

        mock_page.evaluate = AsyncMock(return_value=sample_js_listings)

        adapter = FacebookMarketplaceAdapter()
        adapter.set_scan_mode("direct")
        query = ScanQuery(keywords="PS5", max_price=300.0)

        result = await adapter.scan(query, mock_browser_manager, MagicMock())

        assert isinstance(result, ScanResult)
        assert len(result.listings) == 3
        assert result.listings[0].title == "PS5 Disc Edition"
        # Should NOT have used the browser-use agent
        mock_browser_manager.create_agent.assert_not_called()

    async def test_agent_mode_uses_agent(self, mock_browser_manager):
        """With scan_mode='agent', adapter should use browser-use Agent."""
        from agentic_scraper.sites.facebook.adapter import FacebookMarketplaceAdapter

        # Set up agent mock to return valid data
        listing_json = json.dumps([{"title": "Test", "price": 50.0, "external_id": "1"}])
        history_mock = MagicMock()
        history_mock.extracted_content.return_value = [listing_json]

        agent_mock = AsyncMock()
        agent_mock.run = AsyncMock(return_value=history_mock)
        # create_agent is sync (not awaited), so use MagicMock
        mock_browser_manager.create_agent = MagicMock(return_value=agent_mock)

        adapter = FacebookMarketplaceAdapter()
        adapter.set_scan_mode("agent")
        query = ScanQuery(keywords="test")

        result = await adapter.scan(query, mock_browser_manager, MagicMock())

        assert isinstance(result, ScanResult)
        mock_browser_manager.create_agent.assert_called_once()

    async def test_direct_mode_falls_back_to_agent_on_error(
        self, mock_browser_manager
    ):
        """If direct scan fails, adapter should fall back to agent."""
        from agentic_scraper.sites.facebook.adapter import FacebookMarketplaceAdapter

        # Make get_page fail (simulates browser issue)
        mock_browser_manager.get_page = AsyncMock(
            side_effect=RuntimeError("Page unavailable")
        )

        # Set up agent mock as fallback
        listing_json = json.dumps([
            {"title": "Fallback Item", "price": 100.0, "external_id": "fb_1"}
        ])
        history_mock = MagicMock()
        history_mock.extracted_content.return_value = [listing_json]

        agent_mock = AsyncMock()
        agent_mock.run = AsyncMock(return_value=history_mock)
        # create_agent is sync (not awaited), so use MagicMock
        mock_browser_manager.create_agent = MagicMock(return_value=agent_mock)

        adapter = FacebookMarketplaceAdapter()
        adapter.set_scan_mode("direct")
        query = ScanQuery(keywords="test")

        result = await adapter.scan(query, mock_browser_manager, MagicMock())

        assert isinstance(result, ScanResult)
        assert len(result.listings) == 1
        assert result.listings[0].title == "Fallback Item"
        # Agent should have been used as fallback
        mock_browser_manager.create_agent.assert_called_once()

    async def test_direct_scan_with_llm_fallback(
        self, mock_page, mock_browser_manager
    ):
        """When JS returns empty, LLM fallback should be used."""
        from agentic_scraper.sites.facebook.adapter import FacebookMarketplaceAdapter

        mock_page.evaluate = AsyncMock(return_value=[])
        mock_page._extract_clean_markdown = AsyncMock(
            return_value=("PS5 for $250 in Portland", {})
        )

        mock_llm = MagicMock()
        mock_llm.ainvoke = AsyncMock(
            return_value=MagicMock(content=json.dumps({"listings": [
                {"title": "PS5", "price": 250.0, "external_id": "999"}
            ]}))
        )

        adapter = FacebookMarketplaceAdapter()
        adapter.set_scan_mode("direct")
        adapter.set_json_llm(mock_llm)
        query = ScanQuery(keywords="PS5")

        result = await adapter.scan(query, mock_browser_manager, MagicMock())

        assert isinstance(result, ScanResult)
        assert len(result.listings) == 1
        assert result.listings[0].title == "PS5"

    async def test_adapter_default_scan_mode_is_direct(self):
        """Adapter should default to direct scan mode."""
        from agentic_scraper.sites.facebook.adapter import FacebookMarketplaceAdapter

        adapter = FacebookMarketplaceAdapter()
        assert adapter._scan_mode == "direct"

    async def test_set_json_llm(self):
        """set_json_llm should store the LLM for extraction fallback."""
        from agentic_scraper.sites.facebook.adapter import FacebookMarketplaceAdapter

        adapter = FacebookMarketplaceAdapter()
        mock_llm = MagicMock()
        adapter.set_json_llm(mock_llm)
        assert adapter._json_llm is mock_llm
