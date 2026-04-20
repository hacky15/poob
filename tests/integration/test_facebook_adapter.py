"""Integration tests for the Facebook Marketplace adapter and parser."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from poob.storage.models import Listing


# --- Parser tests ---


class TestParseListings:
    """Tests for parse_listings() from parser.py."""

    def test_parse_valid_json_array(self):
        """Should extract Listing objects from a valid JSON array."""
        from poob.sites.facebook.parser import parse_listings

        raw = json.dumps([
            {
                "title": "PS5 Disc Edition",
                "price": 250.0,
                "location": "Portland, OR",
                "seller_name": "John D.",
                "listing_url": "https://facebook.com/marketplace/item/111",
                "image_url": "https://example.com/ps5.jpg",
                "external_id": "111",
            },
            {
                "title": "Xbox Series X",
                "price": 300.0,
                "location": "Seattle, WA",
                "seller_name": "Jane S.",
                "listing_url": "https://facebook.com/marketplace/item/222",
                "image_url": "https://example.com/xbox.jpg",
                "external_id": "222",
            },
        ])
        listings = parse_listings(raw, site="facebook_marketplace")
        assert len(listings) == 2
        assert listings[0].title == "PS5 Disc Edition"
        assert listings[0].price == 250.0
        assert listings[0].site == "facebook_marketplace"
        assert listings[1].title == "Xbox Series X"

    def test_parse_empty_array(self):
        """Should return empty list for empty JSON array."""
        from poob.sites.facebook.parser import parse_listings

        listings = parse_listings("[]", site="facebook_marketplace")
        assert listings == []

    def test_parse_malformed_json(self):
        """Should return empty list for invalid JSON."""
        from poob.sites.facebook.parser import parse_listings

        listings = parse_listings("not valid json at all", site="facebook_marketplace")
        assert listings == []

    def test_parse_markdown_fenced_json(self):
        """Should extract JSON from markdown code fences."""
        from poob.sites.facebook.parser import parse_listings

        raw = """Here are the listings I found:

```json
[
    {
        "title": "Nintendo Switch",
        "price": 150.0,
        "location": "Portland, OR",
        "external_id": "333"
    }
]
```

That's all I found."""
        listings = parse_listings(raw, site="facebook_marketplace")
        assert len(listings) == 1
        assert listings[0].title == "Nintendo Switch"
        assert listings[0].price == 150.0

    def test_parse_partial_listing_data(self):
        """Should handle listings with missing optional fields."""
        from poob.sites.facebook.parser import parse_listings

        raw = json.dumps([
            {"title": "Some Item", "price": 50.0},
        ])
        listings = parse_listings(raw, site="facebook_marketplace")
        assert len(listings) == 1
        assert listings[0].title == "Some Item"
        assert listings[0].price == 50.0
        assert listings[0].location == ""
        assert listings[0].seller_name == ""

    def test_parse_sets_site_name(self):
        """Each parsed listing should have the correct site set."""
        from poob.sites.facebook.parser import parse_listings

        raw = json.dumps([{"title": "Item", "price": 10.0, "external_id": "x1"}])
        listings = parse_listings(raw, site="facebook_marketplace")
        assert all(l.site == "facebook_marketplace" for l in listings)

    def test_parse_handles_image_url_to_image_urls(self):
        """Parser should convert singular image_url to image_urls list."""
        from poob.sites.facebook.parser import parse_listings

        raw = json.dumps([{
            "title": "Item",
            "price": 10.0,
            "image_url": "https://example.com/img.jpg",
            "external_id": "x2",
        }])
        listings = parse_listings(raw, site="facebook_marketplace")
        assert listings[0].image_urls == ["https://example.com/img.jpg"]


# --- Prompt building tests ---


class TestPromptBuilding:
    """Tests for prompt template building."""

    def test_search_prompt_includes_keywords(self):
        """Search prompt should include the query keywords."""
        from poob.sites.facebook.prompts import build_search_prompt

        prompt = build_search_prompt(keywords="PS5", max_price=None, location=None)
        assert "PS5" in prompt

    def test_search_prompt_includes_max_price(self):
        """Search prompt should include price filter when provided."""
        from poob.sites.facebook.prompts import build_search_prompt

        prompt = build_search_prompt(keywords="PS5", max_price=300.0, location=None)
        assert "300" in prompt

    def test_search_prompt_includes_location(self):
        """Search prompt should include location when provided."""
        from poob.sites.facebook.prompts import build_search_prompt

        prompt = build_search_prompt(
            keywords="PS5", max_price=None, location="Portland, OR"
        )
        assert "Portland" in prompt

    def test_search_prompt_without_optional_filters(self):
        """Search prompt should still work without price/location filters."""
        from poob.sites.facebook.prompts import build_search_prompt

        prompt = build_search_prompt(keywords="laptop", max_price=None, location=None)
        assert "laptop" in prompt
        assert len(prompt) > 50  # Should be a substantive prompt


# --- Adapter integration tests ---


class TestFacebookAdapter:
    """Integration tests for FacebookMarketplaceAdapter."""

    def test_adapter_properties(self):
        """Adapter should have correct site_name, base_url, requires_login."""
        from poob.sites.facebook.adapter import FacebookMarketplaceAdapter

        adapter = FacebookMarketplaceAdapter()
        assert adapter.site_name == "facebook_marketplace"
        assert "facebook.com/marketplace" in adapter.base_url
        assert adapter.requires_login is True

    async def test_scan_returns_scan_result(self):
        """scan() should return a ScanResult with parsed listings (agent mode)."""
        from poob.sites.base import ScanQuery, ScanResult
        from poob.sites.facebook.adapter import FacebookMarketplaceAdapter

        adapter = FacebookMarketplaceAdapter()
        adapter.set_scan_mode("agent")
        query = ScanQuery(keywords="PS5", max_price=300.0)

        # Mock the browser manager and agent
        listing_json = json.dumps([
            {
                "title": "PS5 Console",
                "price": 250.0,
                "location": "Portland, OR",
                "external_id": "fb_999",
                "listing_url": "https://facebook.com/marketplace/item/999",
            }
        ])
        mock_history = MagicMock()
        mock_history.final_result.return_value = listing_json
        mock_history.extracted_content.return_value = [listing_json]
        mock_history.is_successful.return_value = True

        mock_agent = MagicMock()
        mock_agent.run = AsyncMock(return_value=mock_history)

        # create_agent is a sync method, not async - use MagicMock
        mock_browser = MagicMock()
        mock_browser.create_agent.return_value = mock_agent

        mock_llm = MagicMock()

        result = await adapter.scan(query, mock_browser, mock_llm)

        assert isinstance(result, ScanResult)
        assert len(result.listings) == 1
        assert result.listings[0].title == "PS5 Console"
        assert result.listings[0].price == 250.0
        assert len(result.errors) == 0

    async def test_scan_handles_agent_failure(self):
        """scan() should return empty result with errors when agent fails."""
        from poob.sites.base import ScanQuery, ScanResult
        from poob.sites.facebook.adapter import FacebookMarketplaceAdapter

        adapter = FacebookMarketplaceAdapter()
        adapter.set_scan_mode("agent")
        query = ScanQuery(keywords="PS5")

        mock_history = MagicMock()
        mock_history.final_result.return_value = None
        mock_history.extracted_content.return_value = []
        mock_history.is_successful.return_value = False

        mock_agent = MagicMock()
        mock_agent.run = AsyncMock(return_value=mock_history)

        # create_agent is a sync method, not async
        mock_browser = MagicMock()
        mock_browser.create_agent.return_value = mock_agent

        mock_llm = MagicMock()

        result = await adapter.scan(query, mock_browser, mock_llm)

        assert isinstance(result, ScanResult)
        assert len(result.listings) == 0
        assert len(result.errors) > 0
