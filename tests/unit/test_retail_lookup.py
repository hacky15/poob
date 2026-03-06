"""Tests for RetailLookupTool - MSRP/retail price lookup via Tavily."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

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

        mock_search = AsyncMock()
        mock_search.search = AsyncMock(
            return_value="PS5 Disc Edition - $499.99 at Best Buy"
        )

        tool = RetailLookupTool(llm, search_provider=mock_search)
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

        mock_search = AsyncMock()
        mock_search.search = AsyncMock(return_value="No results found")

        tool = RetailLookupTool(llm, search_provider=mock_search)
        result = await tool.run("Unknown Vintage Widget")

        assert result.confidence == 0.0

    async def test_handles_search_failure(self):
        """Should return empty result when search fails."""
        from agentic_scraper.skills.retail_lookup import RetailLookupTool

        llm = MagicMock()

        mock_search = AsyncMock()
        mock_search.search = AsyncMock(side_effect=Exception("API timeout"))

        tool = RetailLookupTool(llm, search_provider=mock_search)
        result = await tool.run("PS5")

        assert result.sample_count == 0
        assert result.confidence == 0.0

    async def test_handles_empty_search_results(self):
        """Should return empty result when search returns no text."""
        from agentic_scraper.skills.retail_lookup import RetailLookupTool

        llm = MagicMock()

        mock_search = AsyncMock()
        mock_search.search = AsyncMock(return_value="")

        tool = RetailLookupTool(llm, search_provider=mock_search)
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

        mock_search = AsyncMock()
        mock_search.search = AsyncMock(return_value="iPhone 15 Pro $999")

        tool = RetailLookupTool(llm, search_provider=mock_search)
        await tool.run("iPhone 15 Pro", brand="Apple", model="iPhone 15 Pro")

        query = mock_search.search.call_args[0][0]
        assert "Apple" in query or "iPhone" in query

    async def test_handles_llm_error(self):
        """Should return zero-confidence when LLM extraction fails."""
        from agentic_scraper.skills.retail_lookup import RetailLookupTool

        llm = MagicMock()
        llm.ainvoke = AsyncMock(side_effect=RuntimeError("LLM error"))

        mock_search = AsyncMock()
        mock_search.search = AsyncMock(return_value="$499 retail price")

        tool = RetailLookupTool(llm, search_provider=mock_search)
        result = await tool.run("PS5")

        assert result.confidence == 0.0

    async def test_parse_llm_json_with_code_fence(self):
        """Should handle LLM responses wrapped in markdown code fences."""
        from agentic_scraper.skills.retail_lookup import RetailLookupTool

        fenced = '```json\n{"retail_price": 299.99, "confidence": 0.8}\n```'
        result = RetailLookupTool._parse_llm_response(fenced, "test query")

        assert result.median_price == 299.99
        assert result.confidence == 0.8
