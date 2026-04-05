"""Tests for SmartDealRadar deal evaluation pipeline."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

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
    # report_vision_result is a sync method — use MagicMock to avoid
    # "coroutine never awaited" warnings when the orchestrator calls it.
    enrichment.report_vision_result = MagicMock()
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
        """When enrichment agrees with listing title, eBay uses enriched name."""
        mock_visual_enrichment.enrich = AsyncMock(
            return_value=_make_enrichment(
                enriched_product_name="IKEA MALM Desk",
                enriched_brand="IKEA",
                enrichment_tier=1,
                enrichment_confidence=0.9,
            )
        )
        # Title contains brand so cross-validation trusts the enriched name
        listing = _make_listing(title="IKEA desk", price=50.0)
        await radar.evaluate_batch([listing])

        mock_ebay_lookup.run.assert_called_once()
        call_query = mock_ebay_lookup.run.call_args[0][0]
        assert "IKEA" in call_query or "MALM" in call_query

    @pytest.mark.asyncio
    async def test_enrichment_divergence_uses_listing_title(
        self, radar, mock_visual_enrichment, mock_ebay_lookup,
    ):
        """When enrichment diverges from listing title, eBay uses listing title."""
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
        assert "brown desk" in call_query
        assert "IKEA" not in call_query

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
            _make_listing(id="listing-1", title="Samsung Smart TV 55 inch", price=100.0),
            _make_listing(id="listing-2", title="Kitchen Table Oak Wood", price=200.0),
            _make_listing(id="listing-3", title="Robotic Vacuum Cleaner", price=50.0),
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


class TestDollarSavingsEnforcement:
    """Test programmatic dollar savings enforcement."""

    def test_incredible_downgraded_when_savings_too_low(self):
        from agentic_scraper.skills.orchestrator import _enforce_dollar_savings

        # $9 savings at 90% off on a $100 item — NOT incredible (needs $75+)
        result = _enforce_dollar_savings(DealScore.INCREDIBLE, 90.0, 9.0, listing_price=100.0)
        assert result == DealScore.FAIR  # Savings too low for any tier

    def test_incredible_stays_when_savings_high(self):
        from agentic_scraper.skills.orchestrator import _enforce_dollar_savings

        # $200 savings at 60% off on a $150 item — IS incredible
        result = _enforce_dollar_savings(DealScore.INCREDIBLE, 60.0, 200.0, listing_price=150.0)
        assert result == DealScore.INCREDIBLE

    def test_great_downgraded_to_good_expensive(self):
        from agentic_scraper.skills.orchestrator import _enforce_dollar_savings

        # $20 savings at 35% off on a $60 item — flat GREAT needs $30, fail.
        # $60 >= $50 so flat thresholds apply. Qualifies for GOOD only.
        result = _enforce_dollar_savings(DealScore.GREAT, 35.0, 20.0, listing_price=60.0)
        assert result == DealScore.GOOD

    def test_great_stays_for_cheap_item(self):
        from agentic_scraper.skills.orchestrator import _enforce_dollar_savings

        # $20 savings at 35% off on a $40 item — proportional GREAT = $10.
        # $20 > $10 and 35% > 30% → stays GREAT.
        result = _enforce_dollar_savings(DealScore.GREAT, 35.0, 20.0, listing_price=40.0)
        assert result == DealScore.GREAT

    def test_good_stays_with_modest_savings(self):
        from agentic_scraper.skills.orchestrator import _enforce_dollar_savings

        # $15 savings at 20% off on a $60 item — meets GOOD thresholds
        result = _enforce_dollar_savings(DealScore.GOOD, 20.0, 15.0, listing_price=60.0)
        assert result == DealScore.GOOD

    def test_fair_unchanged(self):
        from agentic_scraper.skills.orchestrator import _enforce_dollar_savings

        result = _enforce_dollar_savings(DealScore.FAIR, 5.0, 2.0, listing_price=10.0)
        assert result == DealScore.FAIR

    def test_carplay_adapter_example(self):
        """Real example: $10 CarPlay adapter, $30 est value = $20 savings.
        VLM rated INCREDIBLE but it's only $20 saved. At $10 listing price,
        proportional thresholds: GOOD=$1.50, GREAT=$2.50, INCREDIBLE=$4.
        With 67% discount and $20 savings, easily passes INCREDIBLE proportional."""
        from agentic_scraper.skills.orchestrator import _enforce_dollar_savings

        # Cheap item: proportional thresholds kick in. $20 savings at 67%
        # exceeds all proportional thresholds for a $10 item.
        result = _enforce_dollar_savings(DealScore.INCREDIBLE, 67.0, 20.0, listing_price=10.0)
        assert result == DealScore.INCREDIBLE

    def test_penny_sleeves_example(self):
        """Real example: $1 penny sleeves, $9 est value = $8 savings.
        VLM rated INCREDIBLE. At $1 listing price, proportional thresholds
        are tiny: INCREDIBLE=$0.40. Passes easily. But we accept this —
        the proportional system is intentionally generous to cheap items."""
        from agentic_scraper.skills.orchestrator import _enforce_dollar_savings

        # $1 item with $8 savings = 89% off. Proportional INCREDIBLE = $0.40
        result = _enforce_dollar_savings(DealScore.INCREDIBLE, 89.0, 8.0, listing_price=1.0)
        assert result == DealScore.INCREDIBLE

    def test_cheap_item_good_deal_not_penalized(self):
        """A $20 item at 50% off ($10 saved) should qualify as GOOD.
        With flat thresholds ($10 min), this barely passes. But for a $15 item
        at 40% off ($6 saved), flat thresholds kill it. Proportional: $2.25."""
        from agentic_scraper.skills.orchestrator import _enforce_dollar_savings

        # $15 item, $6 savings, 40% discount
        result = _enforce_dollar_savings(DealScore.GOOD, 40.0, 6.0, listing_price=15.0)
        assert result == DealScore.GOOD  # Proportional: $15 * 0.15 = $2.25, passes

    def test_cheap_item_great_deal(self):
        """A $25 item at 60% off ($15 saved) should qualify as GREAT.
        Flat threshold needs $30. Proportional: $25 * 0.25 = $6.25."""
        from agentic_scraper.skills.orchestrator import _enforce_dollar_savings

        result = _enforce_dollar_savings(DealScore.GREAT, 60.0, 15.0, listing_price=25.0)
        assert result == DealScore.GREAT

    def test_expensive_item_uses_flat_thresholds(self):
        """Items $50+ use flat dollar thresholds as before."""
        from agentic_scraper.skills.orchestrator import _enforce_dollar_savings

        # $80 item, $20 savings, 25% off — doesn't meet GREAT ($30) or GOOD (15%+$10)
        # 25% > 15% and $20 > $10 → GOOD
        result = _enforce_dollar_savings(DealScore.GREAT, 25.0, 20.0, listing_price=80.0)
        assert result == DealScore.GOOD

    def test_expensive_item_incredible_needs_75(self):
        """$100 item with $60 savings = 60% off. INCREDIBLE needs $75 flat."""
        from agentic_scraper.skills.orchestrator import _enforce_dollar_savings

        result = _enforce_dollar_savings(DealScore.INCREDIBLE, 60.0, 60.0, listing_price=100.0)
        assert result == DealScore.GREAT  # $60 < $75 for INCREDIBLE, but > $30 for GREAT

    def test_zero_price_uses_flat_thresholds(self):
        """When listing_price is 0 (unknown), use flat thresholds."""
        from agentic_scraper.skills.orchestrator import _enforce_dollar_savings

        result = _enforce_dollar_savings(DealScore.INCREDIBLE, 90.0, 9.0, listing_price=0.0)
        assert result == DealScore.FAIR


class TestMisleadingListingDetection:
    """Test the misleading listing filter."""

    def test_trades_only_detected(self):
        from agentic_scraper.skills.orchestrator import _detect_misleading_listing

        listing = Listing(
            title="Pokemon Cards",
            price=0,
            description="Looking for trades only. Have lots of rare holos.",
        )
        result = _detect_misleading_listing(listing)
        assert result is not None  # Detected as misleading (trades_only or looking_to_trade)

    def test_popup_event_detected(self):
        from agentic_scraper.skills.orchestrator import _detect_misleading_listing

        listing = Listing(
            title="Vintage Clothing Sale",
            price=0,
            description="Come to our pop up this Saturday!",
        )
        result = _detect_misleading_listing(listing)
        assert result == "popup_event"

    def test_legitimate_listing_passes(self):
        from agentic_scraper.skills.orchestrator import _detect_misleading_listing

        listing = Listing(
            title="PS5 Digital Edition",
            price=200,
            description="Great condition, barely used. Comes with controller.",
        )
        result = _detect_misleading_listing(listing)
        assert result is None

    def test_bait_pricing_detected(self):
        from agentic_scraper.skills.orchestrator import _detect_misleading_listing

        listing = Listing(
            title="Furniture",
            price=0,
            description="Prices: couch $200, table $150, chairs $50 each",
        )
        result = _detect_misleading_listing(listing)
        assert result == "hidden_pricing"


class TestPreferenceConstraints:
    """Test positive and negative preference constraints."""

    def test_negation_catches_excluded_term(self):
        from agentic_scraper.skills.orchestrator import _listing_contradicts_notes

        listing = Listing(title="Metal Filing Cabinet", description="Heavy duty steel")
        result = _listing_contradicts_notes(listing, "not metal, prefer wood")
        assert result == "metal"

    def test_positive_constraint_rejects_non_match(self):
        from agentic_scraper.skills.orchestrator import _listing_contradicts_notes

        listing = Listing(title="Beautiful Ceramic Vase Set", description="Set of 3 vases")
        result = _listing_contradicts_notes(listing, "only cups and mugs, no plates, no bowls, no vases")
        assert result is not None  # Should be rejected (either "vases" exclusion or missing "cups")

    def test_positive_constraint_accepts_match(self):
        from agentic_scraper.skills.orchestrator import _listing_contradicts_notes

        listing = Listing(title="Set of 4 ceramic cups", description="Hand-thrown mugs")
        result = _listing_contradicts_notes(listing, "only cups and mugs")
        assert result is None  # Should pass — "cups" is in the title

    def test_no_notes_always_passes(self):
        from agentic_scraper.skills.orchestrator import _listing_contradicts_notes

        listing = Listing(title="Random Item", description="Whatever")
        result = _listing_contradicts_notes(listing, "")
        assert result is None


class TestBulkConstraint:
    """Test bulk/lot preference enforcement."""

    def test_single_card_rejected_when_bulk_required(self):
        from agentic_scraper.skills.orchestrator import _listing_contradicts_notes

        listing = Listing(
            title="Mew [Holo] #4 Pokemon POP Series 4 - MP",
            description="Rare holo card in good condition",
        )
        result = _listing_contradicts_notes(
            listing, "bulk listings, many cards, shoebox, zero sleeves, seller unaware"
        )
        assert result == "not_bulk"

    def test_bulk_lot_passes_when_bulk_required(self):
        from agentic_scraper.skills.orchestrator import _listing_contradicts_notes

        listing = Listing(
            title="Pokemon Cards Huge Lot",
            description="500+ cards in a shoebox, mixed sets",
        )
        result = _listing_contradicts_notes(
            listing, "bulk listings, many cards, shoebox, zero sleeves"
        )
        assert result is None

    def test_collection_passes_when_bulk_required(self):
        from agentic_scraper.skills.orchestrator import _listing_contradicts_notes

        listing = Listing(
            title="Pokemon card collection",
            description="Selling my whole collection, 200+ cards assorted",
        )
        result = _listing_contradicts_notes(
            listing, "bulk listings, many cards"
        )
        assert result is None

    def test_no_bulk_in_notes_allows_single_items(self):
        from agentic_scraper.skills.orchestrator import _listing_contradicts_notes

        listing = Listing(
            title="Mew [Holo] #4 Pokemon POP Series 4",
            description="Single rare card",
        )
        result = _listing_contradicts_notes(listing, "seller unaware, extremely cheap")
        assert result is None  # No bulk constraint in notes


class TestReplacementPartsFilter:
    """Test that replacement parts are filtered when user wants the actual product."""

    def test_gasket_with_part_number_filtered(self):
        from agentic_scraper.skills.orchestrator import _is_replacement_part

        listing = Listing(
            title="New Refrigerator Door Gasket W10830274 for Whirlpool KitchenAid Maytag",
            price=5.0,
        )
        assert _is_replacement_part(listing, "kitchen aid") is True

    def test_water_valve_filtered(self):
        from agentic_scraper.skills.orchestrator import _is_replacement_part

        listing = Listing(
            title="KitchenAid refrigerator water inlet valve",
            price=10.0,
            description="Replacement water inlet valve for KitchenAid fridge",
        )
        assert _is_replacement_part(listing, "kitchen aid") is True

    def test_actual_kitchenaid_mixer_passes(self):
        from agentic_scraper.skills.orchestrator import _is_replacement_part

        listing = Listing(
            title="KitchenAid Artisan 5-Quart Stand Mixer",
            price=150.0,
            description="Barely used, comes with all attachments",
        )
        assert _is_replacement_part(listing, "kitchen aid") is False

    def test_actual_kitchenaid_toaster_passes(self):
        from agentic_scraper.skills.orchestrator import _is_replacement_part

        listing = Listing(
            title="KitchenAid Toaster with multiple settings",
            price=15.0,
            description="Works great, in good condition",
        )
        assert _is_replacement_part(listing, "kitchen aid") is False

    def test_part_number_in_title_filtered(self):
        from agentic_scraper.skills.orchestrator import _is_replacement_part

        listing = Listing(
            title="AP6872729 Dryer Heating Element for Samsung",
            price=12.0,
        )
        assert _is_replacement_part(listing, "samsung") is True

    def test_user_explicitly_wants_parts(self):
        """If user's interest includes 'part', don't filter."""
        from agentic_scraper.skills.orchestrator import _is_replacement_part

        listing = Listing(
            title="KitchenAid Mixer Replacement Paddle",
            price=8.0,
            description="Replacement part for KitchenAid mixer",
        )
        # User explicitly searching for a part
        assert _is_replacement_part(listing, "kitchenaid replacement part") is False

    def test_compatible_with_pattern_filtered(self):
        from agentic_scraper.skills.orchestrator import _is_replacement_part

        listing = Listing(
            title="Refrigerator Water Filter",
            price=8.0,
            description="Compatible with Whirlpool, KitchenAid, Maytag models",
        )
        assert _is_replacement_part(listing, "kitchen aid") is True

    def test_non_watchlist_listing_not_filtered(self):
        """No interest = no filtering (base browse listings)."""
        from agentic_scraper.skills.orchestrator import _is_replacement_part

        listing = Listing(
            title="Door Gasket W10830274",
            price=5.0,
        )
        # Empty interest means base browse — no part filtering
        assert _is_replacement_part(listing, "") is False
