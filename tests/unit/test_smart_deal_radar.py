"""Tests for SmartDealRadar deal evaluation pipeline."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from agentic_scraper.skills.models import (
    PriceLookupResult,
    TriageResult,
    VisualEnrichment,
    VLMEvaluation,
)
from agentic_scraper.storage.models import Deal, DealScore, Listing


def _make_listing(
    id: str = "listing-111",
    title: str = "PS5 Console",
    price: float = 300.0,
    **kwargs,
) -> Listing:
    return Listing(
        id=id,
        site="facebook_marketplace",
        external_id=id.replace("listing-", ""),
        title=title,
        price=price,
        image_urls=kwargs.pop("image_urls", ["https://example.com/img.jpg"]),
        **kwargs,
    )


def _make_triage(investigate: bool = True, **kwargs) -> TriageResult:
    return TriageResult(
        listing_id="listing-111",
        investigate=investigate,
        reasoning="Test triage",
        **kwargs,
    )


def _make_vlm_eval(
    deal_quality: str = "good",
    confidence: float = 0.8,
    estimated_value_mid: float = 500.0,
    **kwargs,
) -> VLMEvaluation:
    return VLMEvaluation(
        item_identified="Test Item",
        condition="good",
        deal_quality=deal_quality,
        confidence=confidence,
        estimated_value_low=estimated_value_mid * 0.8,
        estimated_value_mid=estimated_value_mid,
        estimated_value_high=estimated_value_mid * 1.2,
        reasoning="Test VLM reasoning",
        **kwargs,
    )


def _make_enrichment(**kwargs) -> VisualEnrichment:
    return VisualEnrichment(**kwargs)


@pytest.fixture
def mock_text_triage():
    triage = AsyncMock()
    triage.triage_batch = AsyncMock(return_value=[_make_triage(investigate=True)])
    return triage


@pytest.fixture
def mock_visual_enrichment():
    enrichment = AsyncMock()
    enrichment.enrich = AsyncMock(return_value=_make_enrichment())
    return enrichment


@pytest.fixture
def mock_vlm_evaluator():
    evaluator = AsyncMock()
    evaluator.evaluate = AsyncMock(
        return_value=_make_vlm_eval(deal_quality="good", estimated_value_mid=500.0)
    )
    return evaluator


@pytest.fixture
def mock_ebay_lookup():
    lookup = AsyncMock()
    lookup.run = AsyncMock(
        return_value=PriceLookupResult(
            median_price=450.0, average_price=460.0,
            min_price=350.0, max_price=600.0, sample_count=5,
        )
    )
    return lookup


@pytest.fixture
def radar(mock_text_triage, mock_visual_enrichment, mock_vlm_evaluator, mock_ebay_lookup):
    from agentic_scraper.skills.orchestrator import SmartDealRadar

    return SmartDealRadar(
        text_triage=mock_text_triage,
        visual_enrichment=mock_visual_enrichment,
        vlm_evaluator=mock_vlm_evaluator,
        ebay_lookup=mock_ebay_lookup,
        min_deal_quality="good",
    )


class TestSmartDealRadarV3:
    @pytest.mark.asyncio
    async def test_full_pipeline_flow(
        self, radar, mock_text_triage, mock_visual_enrichment, mock_vlm_evaluator,
    ):
        """Full pipeline: triage → enrich → VLM evaluate."""
        listing = _make_listing(price=200.0)
        results = await radar.evaluate_batch([listing])

        assert len(results) == 1
        deal, vlm_eval = results[0]
        assert deal is not None
        assert deal.score == DealScore.GOOD
        assert vlm_eval.deal_quality == "good"

        mock_text_triage.triage_batch.assert_called_once()
        mock_visual_enrichment.enrich.assert_called_once()
        mock_vlm_evaluator.evaluate.assert_called_once()

    @pytest.mark.asyncio
    async def test_triage_filters_out_listings(
        self, radar, mock_text_triage, mock_vlm_evaluator,
    ):
        """When triage says don't investigate, listing is filtered — no VLM call."""
        mock_text_triage.triage_batch = AsyncMock(
            return_value=[_make_triage(investigate=False)]
        )
        listing = _make_listing()
        results = await radar.evaluate_batch([listing])

        assert len(results) >= 1
        deal, vlm_eval = results[0]
        assert deal is None
        mock_vlm_evaluator.evaluate.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_listings_returns_empty(self, radar):
        """Empty input should return empty output."""
        results = await radar.evaluate_batch([])
        assert results == []

    @pytest.mark.asyncio
    async def test_enrichment_feeds_ebay_with_product_name(
        self, radar, mock_visual_enrichment, mock_ebay_lookup,
    ):
        """When enrichment finds a product name, eBay lookup uses it."""
        mock_visual_enrichment.enrich = AsyncMock(
            return_value=_make_enrichment(
                enriched_product_name="IKEA MALM Desk",
                enriched_brand="IKEA",
                enrichment_tier=1,
                enrichment_confidence=0.9,
            )
        )
        listing = _make_listing(title="brown desk", price=50.0)
        await radar.evaluate_batch([listing])

        mock_ebay_lookup.run.assert_called_once()
        call_query = mock_ebay_lookup.run.call_args[0][0]
        assert "IKEA" in call_query or "MALM" in call_query

    @pytest.mark.asyncio
    async def test_no_enrichment_skips_ebay(
        self, radar, mock_visual_enrichment, mock_ebay_lookup,
    ):
        """When enrichment returns no product name, eBay lookup is skipped."""
        mock_visual_enrichment.enrich = AsyncMock(
            return_value=_make_enrichment(enrichment_tier=3)
        )
        listing = _make_listing(title="random stuff", price=10.0)
        await radar.evaluate_batch([listing])

        mock_ebay_lookup.run.assert_not_called()

    @pytest.mark.asyncio
    async def test_incredible_deal_detected(
        self, radar, mock_vlm_evaluator,
    ):
        """VLM returning 'incredible' should create an INCREDIBLE deal."""
        mock_vlm_evaluator.evaluate = AsyncMock(
            return_value=_make_vlm_eval(deal_quality="incredible", estimated_value_mid=800.0)
        )
        listing = _make_listing(price=100.0)
        results = await radar.evaluate_batch([listing])

        deal, _ = results[0]
        assert deal is not None
        assert deal.score == DealScore.INCREDIBLE

    @pytest.mark.asyncio
    async def test_pass_quality_returns_no_deal(
        self, radar, mock_vlm_evaluator,
    ):
        """VLM returning 'pass' should not create a deal (below min_deal_quality)."""
        mock_vlm_evaluator.evaluate = AsyncMock(
            return_value=_make_vlm_eval(deal_quality="pass", estimated_value_mid=50.0)
        )
        listing = _make_listing(price=50.0)
        results = await radar.evaluate_batch([listing])

        deal, _ = results[0]
        assert deal is None

    @pytest.mark.asyncio
    async def test_fair_quality_returns_no_deal(
        self, radar, mock_vlm_evaluator,
    ):
        """VLM returning 'fair' should not create a deal (min_deal_quality is good)."""
        mock_vlm_evaluator.evaluate = AsyncMock(
            return_value=_make_vlm_eval(deal_quality="fair", estimated_value_mid=100.0)
        )
        listing = _make_listing(price=90.0)
        results = await radar.evaluate_batch([listing])

        deal, _ = results[0]
        assert deal is None

    @pytest.mark.asyncio
    async def test_deal_has_discount_pct(
        self, radar, mock_vlm_evaluator,
    ):
        """Deal should have discount percentage calculated from VLM estimates."""
        mock_vlm_evaluator.evaluate = AsyncMock(
            return_value=_make_vlm_eval(deal_quality="great", estimated_value_mid=400.0)
        )
        listing = _make_listing(price=200.0)
        results = await radar.evaluate_batch([listing])

        deal, _ = results[0]
        assert deal is not None
        assert deal.discount_pct == 50.0
        assert deal.estimated_market_price == 400.0

    @pytest.mark.asyncio
    async def test_free_listing_gets_100_pct_discount(
        self, radar, mock_vlm_evaluator,
    ):
        """Free listing ($0) with identifiable value should get 100% discount."""
        mock_vlm_evaluator.evaluate = AsyncMock(
            return_value=_make_vlm_eval(deal_quality="incredible", estimated_value_mid=200.0)
        )
        listing = _make_listing(price=0.0)
        results = await radar.evaluate_batch([listing])

        deal, _ = results[0]
        assert deal is not None
        assert deal.discount_pct == 100.0

    @pytest.mark.asyncio
    async def test_red_flags_appended_to_reasoning(
        self, radar, mock_vlm_evaluator,
    ):
        """VLM red flags should be included in deal reasoning."""
        mock_vlm_evaluator.evaluate = AsyncMock(
            return_value=_make_vlm_eval(
                deal_quality="good",
                estimated_value_mid=500.0,
                red_flags=["Stock photo detected"],
            )
        )
        listing = _make_listing(price=200.0)
        results = await radar.evaluate_batch([listing])

        deal, _ = results[0]
        assert deal is not None
        assert "Stock photo" in deal.llm_reasoning

    @pytest.mark.asyncio
    async def test_vlm_error_returns_none_deal(
        self, radar, mock_vlm_evaluator,
    ):
        """VLM evaluation error should return None deal, not crash."""
        mock_vlm_evaluator.evaluate = AsyncMock(
            side_effect=Exception("VLM timeout")
        )
        listing = _make_listing()
        results = await radar.evaluate_batch([listing])

        deal, vlm_eval = results[0]
        assert deal is None
        assert "error" in vlm_eval.reasoning.lower() or "VLM" in vlm_eval.reasoning

    @pytest.mark.asyncio
    async def test_enrichment_error_still_evaluates(
        self, radar, mock_visual_enrichment, mock_vlm_evaluator,
    ):
        """Visual enrichment failure should not prevent VLM evaluation."""
        mock_visual_enrichment.enrich = AsyncMock(
            side_effect=Exception("API quota exceeded")
        )
        listing = _make_listing()
        results = await radar.evaluate_batch([listing])

        # VLM evaluator should still be called despite enrichment failure
        mock_vlm_evaluator.evaluate.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_images_skips_enrichment(
        self, radar, mock_visual_enrichment,
    ):
        """Listings without images should skip visual enrichment."""
        listing = _make_listing(image_urls=[])
        await radar.evaluate_batch([listing])

        mock_visual_enrichment.enrich.assert_not_called()

    @pytest.mark.asyncio
    async def test_batch_multiple_listings(
        self, radar, mock_text_triage, mock_vlm_evaluator,
    ):
        """Multiple listings should all go through the pipeline."""
        listings = [
            _make_listing(id="listing-1", title="Item A", price=100.0),
            _make_listing(id="listing-2", title="Item B", price=200.0),
            _make_listing(id="listing-3", title="Item C", price=50.0),
        ]
        mock_text_triage.triage_batch = AsyncMock(
            return_value=[
                _make_triage(investigate=True),
                _make_triage(investigate=False),
                _make_triage(investigate=True),
            ]
        )

        results = await radar.evaluate_batch(listings)

        # 2 of 3 investigated, VLM called for those 2
        assert mock_vlm_evaluator.evaluate.call_count == 2

    @pytest.mark.asyncio
    async def test_watchlist_context_injected(
        self, radar, mock_vlm_evaluator,
    ):
        """Watchlist context should be passed to VLM evaluator for matching listings."""
        from agentic_scraper.storage.models import WatchItem

        listing = _make_listing(title="PS5 Console Bundle", price=300.0)
        interest = WatchItem(
            id="watch-1",
            interest="PS5",
            max_price=500.0,
            discord_user_id="123",
            notification_threshold="good",
        )

        results = await radar.evaluate_batch([listing], watchlist_items=[interest])

        # VLM evaluator should receive watchlist_context
        call_kwargs = mock_vlm_evaluator.evaluate.call_args[1]
        assert call_kwargs.get("watchlist_context") is not None
        assert call_kwargs["watchlist_context"]["interest"] == "PS5"

    @pytest.mark.asyncio
    async def test_watchlist_deal_has_watch_item_id(
        self, radar, mock_vlm_evaluator,
    ):
        """Deals matching watchlist items should have watch_item_id set."""
        from agentic_scraper.storage.models import WatchItem

        mock_vlm_evaluator.evaluate = AsyncMock(
            return_value=_make_vlm_eval(deal_quality="good", estimated_value_mid=500.0)
        )
        listing = _make_listing(title="PS5 Console", price=300.0)
        interest = WatchItem(
            id="watch-1",
            interest="PS5",
            max_price=500.0,
            discord_user_id="123",
        )

        results = await radar.evaluate_batch([listing], watchlist_items=[interest])

        deal, _ = results[0]
        assert deal is not None
        assert deal.watch_item_id == "watch-1"

    @pytest.mark.asyncio
    async def test_single_listing_convenience(
        self, radar,
    ):
        """evaluate() convenience wrapper should work for a single listing."""
        listing = _make_listing(price=200.0)
        deal, vlm_eval = await radar.evaluate(listing)

        # Should work and return a tuple
        assert isinstance(vlm_eval, VLMEvaluation)


class TestQualityToScoreMapping:
    """Test the deal_quality → DealScore mapping."""

    def test_mappings(self):
        from agentic_scraper.skills.orchestrator import _QUALITY_TO_SCORE

        assert _QUALITY_TO_SCORE["pass"] == DealScore.FAIR
        assert _QUALITY_TO_SCORE["fair"] == DealScore.FAIR
        assert _QUALITY_TO_SCORE["good"] == DealScore.GOOD
        assert _QUALITY_TO_SCORE["great"] == DealScore.GREAT
        assert _QUALITY_TO_SCORE["incredible"] == DealScore.INCREDIBLE
