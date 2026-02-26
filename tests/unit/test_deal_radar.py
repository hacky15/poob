"""Tests for DealRadar - LLM-powered autonomous deal scoring."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from agentic_scraper.storage.models import DealScore, Listing


def _make_llm_response(content: str) -> MagicMock:
    """Helper to create a mock LLM response."""
    resp = MagicMock()
    resp.content = content
    return resp


class TestDealRadar:
    """Tests for DealRadar.evaluate()."""

    async def test_evaluate_returns_deals_for_underpriced(self):
        """Should return Deal objects for listings scored GOOD or better."""
        from agentic_scraper.scanner.deal_radar import DealRadar

        llm = MagicMock()
        llm.ainvoke = AsyncMock(return_value=_make_llm_response(json.dumps({
            "estimated_market_price": 400.0,
            "deal_score": "great",
            "reasoning": "PS5 typically sells for $400. This is a great deal.",
        })))

        radar = DealRadar(min_score=DealScore.GOOD)
        listing = Listing(
            id="l1",
            title="PS5 Disc Edition",
            price=250.0,
            description="Like new PS5",
            site="facebook_marketplace",
        )

        deals = await radar.evaluate([listing], llm)
        assert len(deals) == 1
        assert deals[0].score == DealScore.GREAT
        assert deals[0].estimated_market_price == 400.0

    async def test_evaluate_valid_json_response(self):
        """Should correctly parse a valid JSON response from the LLM."""
        from agentic_scraper.scanner.deal_radar import DealRadar

        llm = MagicMock()
        llm.ainvoke = AsyncMock(return_value=_make_llm_response(json.dumps({
            "estimated_market_price": 500.0,
            "deal_score": "incredible",
            "reasoning": "Way underpriced.",
        })))

        radar = DealRadar(min_score=DealScore.GOOD)
        listing = Listing(id="l1", title="Item", price=100.0, site="test")

        deals = await radar.evaluate([listing], llm)
        assert len(deals) == 1
        assert deals[0].score == DealScore.INCREDIBLE
        assert deals[0].llm_reasoning == "Way underpriced."

    async def test_evaluate_malformed_json(self):
        """Should skip listings where LLM returns invalid JSON."""
        from agentic_scraper.scanner.deal_radar import DealRadar

        llm = MagicMock()
        llm.ainvoke = AsyncMock(return_value=_make_llm_response(
            "I think this is a good deal but I can't format JSON properly"
        ))

        radar = DealRadar(min_score=DealScore.GOOD)
        listing = Listing(id="l1", title="Item", price=100.0, site="test")

        deals = await radar.evaluate([listing], llm)
        assert len(deals) == 0

    async def test_evaluate_empty_response(self):
        """Should handle empty LLM response gracefully."""
        from agentic_scraper.scanner.deal_radar import DealRadar

        llm = MagicMock()
        llm.ainvoke = AsyncMock(return_value=_make_llm_response(""))

        radar = DealRadar(min_score=DealScore.GOOD)
        listing = Listing(id="l1", title="Item", price=100.0, site="test")

        deals = await radar.evaluate([listing], llm)
        assert len(deals) == 0

    async def test_evaluate_respects_min_score(self):
        """Listings below min_score should be filtered out."""
        from agentic_scraper.scanner.deal_radar import DealRadar

        llm = MagicMock()
        llm.ainvoke = AsyncMock(return_value=_make_llm_response(json.dumps({
            "estimated_market_price": 120.0,
            "deal_score": "fair",
            "reasoning": "About average price.",
        })))

        radar = DealRadar(min_score=DealScore.GOOD)
        listing = Listing(id="l1", title="Item", price=100.0, site="test")

        deals = await radar.evaluate([listing], llm)
        assert len(deals) == 0  # FAIR < GOOD minimum

    async def test_evaluate_assigns_correct_score(self):
        """Each DealScore value from the LLM should map correctly."""
        from agentic_scraper.scanner.deal_radar import DealRadar

        for score_str, expected_score in [
            ("fair", DealScore.FAIR),
            ("good", DealScore.GOOD),
            ("great", DealScore.GREAT),
            ("incredible", DealScore.INCREDIBLE),
        ]:
            llm = MagicMock()
            llm.ainvoke = AsyncMock(return_value=_make_llm_response(json.dumps({
                "estimated_market_price": 200.0,
                "deal_score": score_str,
                "reasoning": "Test.",
            })))

            radar = DealRadar(min_score=DealScore.FAIR)
            listing = Listing(id="l1", title="Item", price=100.0, site="test")
            deals = await radar.evaluate([listing], llm)
            assert len(deals) == 1
            assert deals[0].score == expected_score

    async def test_prompt_includes_listing_details(self):
        """The LLM prompt should contain the listing title and price."""
        from agentic_scraper.scanner.deal_radar import DealRadar

        llm = MagicMock()
        llm.ainvoke = AsyncMock(return_value=_make_llm_response(json.dumps({
            "estimated_market_price": 400.0,
            "deal_score": "good",
            "reasoning": "Decent deal.",
        })))

        radar = DealRadar(min_score=DealScore.GOOD)
        listing = Listing(
            id="l1",
            title="PlayStation 5 Disc",
            price=250.0,
            description="Barely used console",
            site="test",
        )

        await radar.evaluate([listing], llm)

        # Check the prompt sent to the LLM
        call_args = llm.ainvoke.call_args[0][0]
        prompt_text = str(call_args)
        assert "PlayStation 5 Disc" in prompt_text
        assert "250" in prompt_text

    async def test_evaluate_handles_llm_error(self):
        """Should handle LLM errors without crashing."""
        from agentic_scraper.scanner.deal_radar import DealRadar

        llm = MagicMock()
        llm.ainvoke = AsyncMock(side_effect=RuntimeError("LLM timeout"))

        radar = DealRadar(min_score=DealScore.GOOD)
        listing = Listing(id="l1", title="Item", price=100.0, site="test")

        deals = await radar.evaluate([listing], llm)
        assert len(deals) == 0
