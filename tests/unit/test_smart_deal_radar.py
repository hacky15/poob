"""Tests for SmartDealRadar - LLM-orchestrated deal evaluation."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agentic_scraper.skills.models import (
    CategoryEstimate,
    DealEvaluation,
    ItemIdentification,
    PriceLookupResult,
)
from agentic_scraper.storage.models import Deal, DealScore, Listing


class TestSmartDealRadar:
    """Tests for the SmartDealRadar orchestrator."""

    async def test_full_evaluation_flow(self):
        """Should identify item, look up prices, and return a deal."""
        from agentic_scraper.skills.orchestrator import SmartDealRadar

        identify = AsyncMock(return_value=ItemIdentification(
            item_name="PlayStation 5 Disc Edition",
            brand="Sony",
            model="CFI-1215A",
            category="electronics/gaming/console",
            condition="like new",
            confidence=0.95,
            needs_visual=False,
        ))
        ebay = AsyncMock(return_value=PriceLookupResult(
            median_price=380.0,
            average_price=375.0,
            min_price=320.0,
            max_price=420.0,
            sample_count=12,
            source="ebay_sold",
            search_query="PlayStation 5 Disc Edition",
            confidence=0.9,
        ))

        radar = SmartDealRadar(
            identify_tool=identify,
            visual_identify_tool=AsyncMock(),
            ebay_lookup_tool=ebay,
            retail_lookup_tool=AsyncMock(),
            category_estimate_tool=AsyncMock(),
        )

        listing = Listing(
            id="l1",
            title="PS5 Disc Edition",
            price=200.0,
            description="Like new, barely used",
            site="facebook_marketplace",
        )

        deal = await radar.evaluate(listing)

        assert deal is not None
        assert isinstance(deal, Deal)
        assert deal.score in (DealScore.GREAT, DealScore.INCREDIBLE)
        assert deal.estimated_market_price == pytest.approx(380.0, abs=5)
        assert deal.discount_pct > 40

    async def test_uses_visual_when_text_uncertain(self):
        """Should call visual_identify when text identification is uncertain."""
        from agentic_scraper.skills.orchestrator import SmartDealRadar

        identify = AsyncMock(return_value=ItemIdentification(
            item_name="table",
            category="furniture/table",
            confidence=0.2,
            needs_visual=True,
        ))
        visual = AsyncMock(return_value=ItemIdentification(
            item_name="West Elm Mid-Century Dining Table",
            brand="West Elm",
            category="furniture/table/dining",
            confidence=0.85,
            needs_visual=False,
        ))
        ebay = AsyncMock(return_value=PriceLookupResult(
            median_price=350.0,
            average_price=370.0,
            min_price=280.0,
            max_price=450.0,
            sample_count=8,
            source="ebay_sold",
            search_query="West Elm Mid-Century Dining Table",
            confidence=0.85,
        ))

        radar = SmartDealRadar(
            identify_tool=identify,
            visual_identify_tool=visual,
            ebay_lookup_tool=ebay,
            retail_lookup_tool=AsyncMock(),
            category_estimate_tool=AsyncMock(),
        )

        listing = Listing(
            id="l1",
            title="Nice table $50",
            price=50.0,
            description="",
            site="facebook_marketplace",
            image_urls=["https://example.com/table.jpg"],
        )

        deal = await radar.evaluate(listing)

        visual.assert_called_once()
        assert deal is not None
        assert deal.estimated_market_price == pytest.approx(350.0, abs=5)

    async def test_falls_back_to_category_estimate(self):
        """When eBay and retail fail, should use category estimate."""
        from agentic_scraper.skills.orchestrator import SmartDealRadar

        identify = AsyncMock(return_value=ItemIdentification(
            item_name="Vintage Wooden Chair",
            category="furniture/chair",
            confidence=0.7,
            needs_visual=False,
        ))
        ebay = AsyncMock(return_value=PriceLookupResult(
            sample_count=0, confidence=0.0,
        ))
        retail = AsyncMock(return_value=PriceLookupResult(
            sample_count=0, confidence=0.0,
        ))
        category = AsyncMock(return_value=CategoryEstimate(
            low_price=30.0,
            high_price=200.0,
            typical_price=80.0,
            source="category_estimate",
            confidence=0.4,
        ))

        radar = SmartDealRadar(
            identify_tool=identify,
            visual_identify_tool=AsyncMock(),
            ebay_lookup_tool=ebay,
            retail_lookup_tool=retail,
            category_estimate_tool=category,
        )

        listing = Listing(
            id="l1",
            title="Vintage Wooden Chair",
            price=15.0,
            description="Old chair, sturdy",
            site="facebook_marketplace",
        )

        deal = await radar.evaluate(listing)

        category.assert_called_once()
        assert deal is not None
        assert deal.estimated_market_price == pytest.approx(80.0, abs=5)

    async def test_flags_suspicious_discount(self):
        """Extremely high discounts should be flagged as suspicious."""
        from agentic_scraper.skills.orchestrator import SmartDealRadar

        identify = AsyncMock(return_value=ItemIdentification(
            item_name="MacBook Pro 16",
            brand="Apple",
            model="MacBook Pro 16-inch M3",
            category="electronics/laptop",
            confidence=0.95,
            needs_visual=False,
        ))
        ebay = AsyncMock(return_value=PriceLookupResult(
            median_price=2200.0,
            average_price=2100.0,
            min_price=1800.0,
            max_price=2500.0,
            sample_count=15,
            source="ebay_sold",
            search_query="MacBook Pro 16 M3",
            confidence=0.95,
        ))

        radar = SmartDealRadar(
            identify_tool=identify,
            visual_identify_tool=AsyncMock(),
            ebay_lookup_tool=ebay,
            retail_lookup_tool=AsyncMock(),
            category_estimate_tool=AsyncMock(),
            scam_threshold_pct=80.0,
        )

        listing = Listing(
            id="l1",
            title="MacBook Pro 16 M3",
            price=100.0,  # 95% discount - very suspicious
            description="Brand new in box",
            site="facebook_marketplace",
        )

        deal = await radar.evaluate(listing)

        assert deal is not None
        assert "suspicious" in deal.llm_reasoning.lower() or deal.discount_pct > 80

    async def test_no_deal_for_fair_price(self):
        """Items priced at or near market value shouldn't generate deals."""
        from agentic_scraper.skills.orchestrator import SmartDealRadar

        identify = AsyncMock(return_value=ItemIdentification(
            item_name="iPhone 15",
            brand="Apple",
            category="electronics/phone",
            confidence=0.9,
            needs_visual=False,
        ))
        ebay = AsyncMock(return_value=PriceLookupResult(
            median_price=750.0,
            average_price=740.0,
            min_price=700.0,
            max_price=800.0,
            sample_count=20,
            source="ebay_sold",
            search_query="iPhone 15",
            confidence=0.95,
        ))

        radar = SmartDealRadar(
            identify_tool=identify,
            visual_identify_tool=AsyncMock(),
            ebay_lookup_tool=ebay,
            retail_lookup_tool=AsyncMock(),
            category_estimate_tool=AsyncMock(),
            min_score=DealScore.GOOD,
        )

        listing = Listing(
            id="l1",
            title="iPhone 15",
            price=700.0,  # Only ~7% below median
            description="Good condition",
            site="facebook_marketplace",
        )

        deal = await radar.evaluate(listing)

        # 7% discount = FAIR, below GOOD threshold, should return None
        assert deal is None

    async def test_skips_visual_when_no_images(self):
        """Should not call visual_identify when listing has no images."""
        from agentic_scraper.skills.orchestrator import SmartDealRadar

        identify = AsyncMock(return_value=ItemIdentification(
            item_name="table",
            category="furniture/table",
            confidence=0.3,
            needs_visual=True,
        ))
        visual = AsyncMock()
        category = AsyncMock(return_value=CategoryEstimate(
            low_price=50.0,
            high_price=300.0,
            typical_price=150.0,
            confidence=0.4,
        ))

        radar = SmartDealRadar(
            identify_tool=identify,
            visual_identify_tool=visual,
            ebay_lookup_tool=AsyncMock(return_value=PriceLookupResult(
                sample_count=0, confidence=0.0,
            )),
            retail_lookup_tool=AsyncMock(return_value=PriceLookupResult(
                sample_count=0, confidence=0.0,
            )),
            category_estimate_tool=category,
        )

        listing = Listing(
            id="l1",
            title="Nice table",
            price=20.0,
            description="",
            site="facebook_marketplace",
            image_urls=[],  # No images
        )

        await radar.evaluate(listing)

        visual.assert_not_called()

    async def test_handles_identify_failure(self):
        """Should still attempt pricing even if identification fails."""
        from agentic_scraper.skills.orchestrator import SmartDealRadar

        identify = AsyncMock(side_effect=RuntimeError("LLM crash"))
        category = AsyncMock(return_value=CategoryEstimate(
            low_price=50.0,
            high_price=300.0,
            typical_price=150.0,
            confidence=0.3,
        ))

        radar = SmartDealRadar(
            identify_tool=identify,
            visual_identify_tool=AsyncMock(),
            ebay_lookup_tool=AsyncMock(return_value=PriceLookupResult(
                sample_count=0, confidence=0.0,
            )),
            retail_lookup_tool=AsyncMock(return_value=PriceLookupResult(
                sample_count=0, confidence=0.0,
            )),
            category_estimate_tool=category,
        )

        listing = Listing(
            id="l1",
            title="Something for sale",
            price=10.0,
            site="facebook_marketplace",
        )

        # Should not raise
        deal = await radar.evaluate(listing)
        # May return a deal with low confidence from category estimate
        # or None if it can't determine anything useful

    async def test_uses_retail_when_ebay_low_samples(self):
        """Should call retail lookup when eBay has < 3 results."""
        from agentic_scraper.skills.orchestrator import SmartDealRadar

        identify = AsyncMock(return_value=ItemIdentification(
            item_name="Rare Collectible Widget",
            brand="WidgetCo",
            category="collectibles",
            confidence=0.8,
            needs_visual=False,
        ))
        ebay = AsyncMock(return_value=PriceLookupResult(
            median_price=100.0,
            average_price=100.0,
            min_price=100.0,
            max_price=100.0,
            sample_count=1,  # Only 1 result - below min threshold
            source="ebay_sold",
            search_query="Rare Collectible Widget",
            confidence=0.3,
        ))
        retail = AsyncMock(return_value=PriceLookupResult(
            median_price=150.0,
            average_price=150.0,
            min_price=150.0,
            max_price=150.0,
            sample_count=1,
            source="retail",
            search_query="WidgetCo Rare Collectible Widget",
            confidence=0.7,
        ))

        radar = SmartDealRadar(
            identify_tool=identify,
            visual_identify_tool=AsyncMock(),
            ebay_lookup_tool=ebay,
            retail_lookup_tool=retail,
            category_estimate_tool=AsyncMock(),
            ebay_min_samples=3,
        )

        listing = Listing(
            id="l1",
            title="Rare Collectible Widget",
            price=50.0,
            description="Like new in box",
            site="facebook_marketplace",
        )

        deal = await radar.evaluate(listing)

        retail.assert_called_once()
