"""Tests for CategoryEstimateTool - category-level pricing fallback."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from agentic_scraper.skills.models import CategoryEstimate


def _make_llm_response(data: dict) -> MagicMock:
    resp = MagicMock()
    resp.content = json.dumps(data)
    return resp


class TestCategoryEstimateTool:
    """Tests for the category estimate skill."""

    async def test_returns_price_range_for_category(self):
        """Should return low/high/typical prices for a category."""
        from agentic_scraper.skills.category_estimate import CategoryEstimateTool

        llm = MagicMock()
        llm.ainvoke = AsyncMock(return_value=_make_llm_response({
            "low_price": 150.0,
            "high_price": 600.0,
            "typical_price": 350.0,
            "confidence": 0.5,
        }))

        tool = CategoryEstimateTool(llm)
        result = await tool.run("furniture/table", condition="good")

        assert isinstance(result, CategoryEstimate)
        assert result.low_price == 150.0
        assert result.high_price == 600.0
        assert result.typical_price == 350.0
        assert result.source == "category_estimate"

    async def test_confidence_always_below_data_backed(self):
        """Category estimates should never have confidence > 0.6."""
        from agentic_scraper.skills.category_estimate import CategoryEstimateTool

        llm = MagicMock()
        llm.ainvoke = AsyncMock(return_value=_make_llm_response({
            "low_price": 100.0,
            "high_price": 500.0,
            "typical_price": 300.0,
            "confidence": 0.95,  # LLM claims high confidence
        }))

        tool = CategoryEstimateTool(llm)
        result = await tool.run("electronics/phone")

        # Should be capped - category estimates are inherently lower confidence
        assert result.confidence <= 0.6

    async def test_includes_condition_in_prompt(self):
        """Should pass condition to LLM for condition-adjusted pricing."""
        from agentic_scraper.skills.category_estimate import CategoryEstimateTool

        llm = MagicMock()
        llm.ainvoke = AsyncMock(return_value=_make_llm_response({
            "low_price": 50.0,
            "high_price": 200.0,
            "typical_price": 100.0,
            "confidence": 0.4,
        }))

        tool = CategoryEstimateTool(llm)
        await tool.run(
            "electronics/gaming/console",
            condition="parts",
            description_hints="broken, for parts only",
        )

        prompt_text = str(llm.ainvoke.call_args[0][0])
        assert "parts" in prompt_text.lower()

    async def test_handles_llm_error(self):
        """Should return zero-confidence estimate on LLM failure."""
        from agentic_scraper.skills.category_estimate import CategoryEstimateTool

        llm = MagicMock()
        llm.ainvoke = AsyncMock(side_effect=RuntimeError("LLM down"))

        tool = CategoryEstimateTool(llm)
        result = await tool.run("electronics/phone")

        assert isinstance(result, CategoryEstimate)
        assert result.confidence == 0.0
        assert result.typical_price == 0.0

    async def test_handles_malformed_response(self):
        """Should handle non-JSON LLM responses."""
        from agentic_scraper.skills.category_estimate import CategoryEstimateTool

        llm = MagicMock()
        llm.ainvoke = AsyncMock(return_value=MagicMock(
            content="I'm not sure about the pricing"
        ))

        tool = CategoryEstimateTool(llm)
        result = await tool.run("furniture/chair")

        assert result.confidence == 0.0

    async def test_description_hints_included(self):
        """Should pass description hints to improve estimate."""
        from agentic_scraper.skills.category_estimate import CategoryEstimateTool

        llm = MagicMock()
        llm.ainvoke = AsyncMock(return_value=_make_llm_response({
            "low_price": 500.0,
            "high_price": 2000.0,
            "typical_price": 1000.0,
            "confidence": 0.5,
        }))

        tool = CategoryEstimateTool(llm)
        await tool.run(
            "furniture/table",
            description_hints="mid-century modern, solid wood, 6-person dining",
        )

        prompt_text = str(llm.ainvoke.call_args[0][0])
        assert "mid-century" in prompt_text.lower() or "solid wood" in prompt_text.lower()
