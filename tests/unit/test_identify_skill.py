"""Tests for IdentifyItemTool - text-based item identification."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from agentic_scraper.skills.models import ItemIdentification, clean_optional


def _make_llm_response(data: dict) -> MagicMock:
    """Helper to create a mock LLM response with structured JSON."""
    resp = MagicMock()
    resp.content = json.dumps(data)
    return resp


class TestIdentifyItemTool:
    """Tests for the identify_item skill."""

    async def test_identifies_specific_product(self):
        """Should extract brand, model, and category from clear title."""
        from agentic_scraper.skills.identify import IdentifyItemTool

        llm = MagicMock()
        llm.ainvoke = AsyncMock(return_value=_make_llm_response({
            "item_name": "PlayStation 5 Disc Edition",
            "brand": "Sony",
            "model": "CFI-1215A",
            "category": "electronics/gaming/console",
            "condition": "like new",
            "confidence": 0.95,
            "needs_visual": False,
        }))

        tool = IdentifyItemTool(llm)
        result = await tool.run("PS5 Disc Edition - barely used", "Like new, barely used")

        assert isinstance(result, ItemIdentification)
        assert result.item_name == "PlayStation 5 Disc Edition"
        assert result.brand == "Sony"
        assert result.category == "electronics/gaming/console"
        assert result.confidence >= 0.9
        assert result.needs_visual is False

    async def test_vague_title_flags_needs_visual(self):
        """Vague titles should have low confidence and needs_visual=True."""
        from agentic_scraper.skills.identify import IdentifyItemTool

        llm = MagicMock()
        llm.ainvoke = AsyncMock(return_value=_make_llm_response({
            "item_name": "table",
            "brand": None,
            "model": None,
            "category": "furniture/table",
            "condition": None,
            "confidence": 0.2,
            "needs_visual": True,
        }))

        tool = IdentifyItemTool(llm)
        result = await tool.run("Nice table $50", "")

        assert result.confidence < 0.5
        assert result.needs_visual is True
        assert result.category == "furniture/table"

    async def test_detects_condition_from_text(self):
        """Should extract condition keywords like 'broken', 'for parts'."""
        from agentic_scraper.skills.identify import IdentifyItemTool

        llm = MagicMock()
        llm.ainvoke = AsyncMock(return_value=_make_llm_response({
            "item_name": "iPhone 13",
            "brand": "Apple",
            "model": "iPhone 13",
            "category": "electronics/phone",
            "condition": "parts",
            "confidence": 0.8,
            "needs_visual": False,
        }))

        tool = IdentifyItemTool(llm)
        result = await tool.run(
            "iPhone 13 - FOR PARTS", "Screen cracked, doesn't turn on"
        )

        assert result.condition == "parts"

    async def test_handles_llm_error_gracefully(self):
        """Should return low-confidence result when LLM fails."""
        from agentic_scraper.skills.identify import IdentifyItemTool

        llm = MagicMock()
        llm.ainvoke = AsyncMock(side_effect=RuntimeError("LLM timeout"))

        tool = IdentifyItemTool(llm)
        result = await tool.run("PS5 Disc Edition", "Like new")

        assert isinstance(result, ItemIdentification)
        assert result.confidence == 0.0
        assert result.needs_visual is True

    async def test_handles_malformed_json(self):
        """Should return low-confidence result when LLM returns bad JSON."""
        from agentic_scraper.skills.identify import IdentifyItemTool

        llm = MagicMock()
        llm.ainvoke = AsyncMock(return_value=_make_llm_response({}))
        # Override with non-JSON text
        llm.ainvoke.return_value.content = "I think this is a table"

        tool = IdentifyItemTool(llm)
        result = await tool.run("Nice table", "")

        assert result.confidence == 0.0
        assert result.needs_visual is True

    async def test_passes_title_and_description_to_llm(self):
        """The LLM prompt should contain both title and description."""
        from agentic_scraper.skills.identify import IdentifyItemTool

        llm = MagicMock()
        llm.ainvoke = AsyncMock(return_value=_make_llm_response({
            "item_name": "test",
            "brand": None,
            "model": None,
            "category": "other",
            "condition": None,
            "confidence": 0.5,
            "needs_visual": False,
        }))

        tool = IdentifyItemTool(llm)
        await tool.run("Xbox Series X", "Barely used, comes with controller")

        call_args = llm.ainvoke.call_args[0][0]
        prompt_text = str(call_args)
        assert "Xbox Series X" in prompt_text
        assert "Barely used" in prompt_text

    async def test_distinguishes_accessory_from_main_item(self):
        """Should correctly identify 'PS5 controller' as controller, not console."""
        from agentic_scraper.skills.identify import IdentifyItemTool

        llm = MagicMock()
        llm.ainvoke = AsyncMock(return_value=_make_llm_response({
            "item_name": "PS5 DualSense Controller",
            "brand": "Sony",
            "model": "DualSense",
            "category": "electronics/gaming/accessory",
            "condition": "good",
            "confidence": 0.9,
            "needs_visual": False,
        }))

        tool = IdentifyItemTool(llm)
        result = await tool.run("PS5 controller", "White DualSense, works great")

        assert "controller" in result.item_name.lower() or "dualsense" in result.item_name.lower()
        assert "accessory" in result.category

    async def test_detects_urgency_signals(self):
        """Should extract urgency signals from listing text."""
        from agentic_scraper.skills.identify import IdentifyItemTool

        llm = MagicMock()
        llm.ainvoke = AsyncMock(return_value=_make_llm_response({
            "item_name": "KitchenAid Stand Mixer",
            "brand": "KitchenAid",
            "model": None,
            "category": "appliances/kitchen",
            "condition": "good",
            "confidence": 0.85,
            "needs_visual": False,
            "urgency_signals": ["must sell", "moving sale", "obo"],
        }))

        tool = IdentifyItemTool(llm)
        result = await tool.run(
            "KitchenAid Mixer - MUST SELL moving sale OBO",
            "Moving next week, need it gone. Make an offer.",
        )

        assert isinstance(result.urgency_signals, tuple)
        assert len(result.urgency_signals) == 3
        assert "must sell" in result.urgency_signals
        assert "moving sale" in result.urgency_signals
        assert "obo" in result.urgency_signals

    async def test_empty_urgency_signals(self):
        """Should return empty tuple when no urgency signals found."""
        from agentic_scraper.skills.identify import IdentifyItemTool

        llm = MagicMock()
        llm.ainvoke = AsyncMock(return_value=_make_llm_response({
            "item_name": "PS5",
            "brand": "Sony",
            "category": "electronics/gaming",
            "confidence": 0.9,
            "needs_visual": False,
            "urgency_signals": [],
        }))

        tool = IdentifyItemTool(llm)
        result = await tool.run("PS5 Disc Edition", "Like new, barely used")

        assert result.urgency_signals == ()

    async def test_missing_urgency_signals_field(self):
        """Should default to empty tuple when LLM omits urgency_signals."""
        from agentic_scraper.skills.identify import IdentifyItemTool

        llm = MagicMock()
        llm.ainvoke = AsyncMock(return_value=_make_llm_response({
            "item_name": "Table",
            "category": "furniture",
            "confidence": 0.7,
            "needs_visual": False,
        }))

        tool = IdentifyItemTool(llm)
        result = await tool.run("Table", "Nice wooden table")

        assert result.urgency_signals == ()

    async def test_null_string_brand_sanitized(self):
        """LLM returning the string 'null' for brand should become None."""
        from agentic_scraper.skills.identify import IdentifyItemTool

        llm = MagicMock()
        llm.ainvoke = AsyncMock(return_value=_make_llm_response({
            "item_name": "Gas Powered Scooter",
            "brand": "null",
            "model": "null",
            "category": "vehicles/scooter",
            "condition": "parts",
            "confidence": 0.8,
            "needs_visual": False,
        }))

        tool = IdentifyItemTool(llm)
        result = await tool.run("Nope", "Gas scooter for parts")

        assert result.brand is None
        assert result.model is None

    async def test_none_string_brand_sanitized(self):
        """LLM returning the string 'None' for brand should become None."""
        from agentic_scraper.skills.identify import IdentifyItemTool

        llm = MagicMock()
        llm.ainvoke = AsyncMock(return_value=_make_llm_response({
            "item_name": "Dining Table",
            "brand": "None",
            "model": "N/A",
            "category": "furniture/table",
            "condition": "unknown",
            "confidence": 0.6,
            "needs_visual": False,
        }))

        tool = IdentifyItemTool(llm)
        result = await tool.run("Dining Table", "Wooden table")

        assert result.brand is None
        assert result.model is None
        assert result.condition is None  # "unknown" sanitized to None


class TestCleanOptional:
    """Tests for the clean_optional sanitizer."""

    def test_none_passthrough(self):
        assert clean_optional(None) is None

    def test_real_string_passthrough(self):
        assert clean_optional("Sony") == "Sony"
        assert clean_optional("DeLonghi") == "DeLonghi"

    def test_null_string_becomes_none(self):
        assert clean_optional("null") is None
        assert clean_optional("Null") is None
        assert clean_optional("NULL") is None

    def test_none_string_becomes_none(self):
        assert clean_optional("None") is None
        assert clean_optional("none") is None

    def test_na_strings_become_none(self):
        assert clean_optional("N/A") is None
        assert clean_optional("n/a") is None
        assert clean_optional("NA") is None
        assert clean_optional("na") is None

    def test_unknown_string_becomes_none(self):
        assert clean_optional("unknown") is None
        assert clean_optional("Unknown") is None

    def test_empty_string_becomes_none(self):
        assert clean_optional("") is None

    def test_whitespace_trimmed(self):
        assert clean_optional("  Sony  ") == "Sony"
        assert clean_optional("  null  ") is None
