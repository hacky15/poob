"""Tests for RetailLookupTool - MSRP/retail price lookup."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agentic_scraper.skills.models import PriceLookupResult


class TestRetailLookupTool:
    """Tests for the retail price lookup skill."""

    async def test_extracts_price_from_search_results(self):
        """Should find retail price from web search results."""
        from agentic_scraper.skills.retail_lookup import RetailLookupTool

        llm = MagicMock()
        llm.ainvoke = AsyncMock(return_value=MagicMock(content=json.dumps({
            "retail_price": 499.99,
            "source_description": "Official Sony retail price",
            "confidence": 0.9,
        })))

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = "<html><body>PS5 Disc Edition - $499.99 at Best Buy</body></html>"

        with patch("agentic_scraper.skills.retail_lookup.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            tool = RetailLookupTool(llm)
            result = await tool.run("PlayStation 5 Disc Edition", brand="Sony")

        assert isinstance(result, PriceLookupResult)
        assert result.median_price == 499.99
        assert result.source == "retail"

    async def test_handles_no_price_found(self):
        """Should return zero-confidence result when no price is found."""
        from agentic_scraper.skills.retail_lookup import RetailLookupTool

        llm = MagicMock()
        llm.ainvoke = AsyncMock(return_value=MagicMock(content=json.dumps({
            "retail_price": 0,
            "source_description": "Could not find price",
            "confidence": 0.0,
        })))

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = "<html><body>No results</body></html>"

        with patch("agentic_scraper.skills.retail_lookup.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            tool = RetailLookupTool(llm)
            result = await tool.run("Unknown Vintage Widget")

        assert result.confidence == 0.0

    async def test_handles_http_failure(self):
        """Should return empty result when HTTP request fails."""
        from agentic_scraper.skills.retail_lookup import RetailLookupTool

        llm = MagicMock()

        with patch("agentic_scraper.skills.retail_lookup.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(side_effect=Exception("Connection timeout"))
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            tool = RetailLookupTool(llm)
            result = await tool.run("PS5")

        assert result.sample_count == 0
        assert result.confidence == 0.0

    async def test_builds_search_query_with_brand(self):
        """Should include brand in search query when provided."""
        from agentic_scraper.skills.retail_lookup import RetailLookupTool

        llm = MagicMock()
        llm.ainvoke = AsyncMock(return_value=MagicMock(content=json.dumps({
            "retail_price": 999.0,
            "source_description": "Apple Store",
            "confidence": 0.9,
        })))

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = "<html><body>$999</body></html>"

        with patch("agentic_scraper.skills.retail_lookup.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            tool = RetailLookupTool(llm)
            await tool.run("iPhone 15 Pro", brand="Apple", model="iPhone 15 Pro")

            call_url = mock_client.get.call_args[0][0]
            assert "Apple" in call_url or "iPhone" in call_url

    async def test_handles_llm_error(self):
        """Should return zero-confidence when LLM extraction fails."""
        from agentic_scraper.skills.retail_lookup import RetailLookupTool

        llm = MagicMock()
        llm.ainvoke = AsyncMock(side_effect=RuntimeError("LLM error"))

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = "<html><body>$499</body></html>"

        with patch("agentic_scraper.skills.retail_lookup.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            tool = RetailLookupTool(llm)
            result = await tool.run("PS5")

        assert result.confidence == 0.0
