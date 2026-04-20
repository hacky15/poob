"""Tests for Discord embed formatters."""

from __future__ import annotations

import discord
import pytest

from poob.storage.models import Deal, DealScore, Listing, WatchItem


class TestFormatDealEmbed:
    """Tests for format_deal_embed()."""

    def test_deal_embed_has_title_and_fields(self):
        """Deal embed should contain the listing title and key fields."""
        from poob.discord_bot.formatter import format_deal_embed

        listing = Listing(
            title="PS5 Disc Edition",
            price=250.0,
            location="Portland, OR",
            listing_url="https://facebook.com/marketplace/item/123",
            site="facebook_marketplace",
        )
        deal = Deal(
            listing_id="l1",
            score=DealScore.GREAT,
            estimated_market_price=400.0,
            discount_pct=37.5,
            llm_reasoning="Good deal on a PS5.",
        )

        embed = format_deal_embed(deal, listing)
        assert isinstance(embed, discord.Embed)
        assert "PS5 Disc Edition" in embed.title
        field_names = [f.name for f in embed.fields]
        assert "Price" in field_names
        assert "Location" in field_names

    def test_deal_embed_color_by_score(self):
        """Embed color should change based on DealScore."""
        from poob.discord_bot.formatter import format_deal_embed, SCORE_COLORS

        listing = Listing(title="Item", price=10.0)
        for score, expected_color in SCORE_COLORS.items():
            deal = Deal(listing_id="l1", score=score)
            embed = format_deal_embed(deal, listing)
            assert embed.color.value == expected_color.value

    def test_deal_embed_includes_link(self):
        """Deal embed should link to the listing."""
        from poob.discord_bot.formatter import format_deal_embed

        listing = Listing(
            title="Item",
            price=10.0,
            listing_url="https://example.com/item/1",
        )
        deal = Deal(listing_id="l1", score=DealScore.GOOD)
        embed = format_deal_embed(deal, listing)
        assert "https://example.com/item/1" in (embed.url or "")

    def test_deal_embed_handles_missing_image(self):
        """Embed should not crash when listing has no images."""
        from poob.discord_bot.formatter import format_deal_embed

        listing = Listing(title="Item", price=10.0, image_urls=[])
        deal = Deal(listing_id="l1", score=DealScore.FAIR)
        embed = format_deal_embed(deal, listing)
        # No image should be set when listing has no images
        assert embed.image is None or embed.image.url is None or embed.image.url == ""


class TestProvenanceField:
    """Tests for the provenance 'Evaluation Details' in deal embeds."""

    def test_provenance_shown_when_available(self):
        from poob.discord_bot.formatter import format_deal_embed
        from poob.skills.models import DealProvenance

        prov = DealProvenance(
            vlm_providers=["gemini_flash_lite", "groq_vision"],
            vlm_agreement=0.85,
            vlm_condition="good",
            vlm_condition_notes="minor scratches",
            vlm_item_identified="KitchenAid Artisan Mixer",
            price_source="ebay_sold",
            price_sample_count=5,
            price_confidence=0.8,
            final_score="great",
        )
        listing = Listing(title="KitchenAid Mixer", price=50.0)
        deal = Deal(
            listing_id="l1",
            score=DealScore.GREAT,
            provenance_json=prov.to_json(),
        )
        embed = format_deal_embed(deal, listing)
        field_names = [f.name for f in embed.fields]
        assert "Evaluation Details" in field_names
        details_field = next(f for f in embed.fields if f.name == "Evaluation Details")
        assert "gemini_flash_lite" in details_field.value
        assert "groq_vision" in details_field.value
        assert "agreement: 0.85" in details_field.value
        assert "ebay_sold" in details_field.value
        assert "KitchenAid Artisan Mixer" in details_field.value

    def test_provenance_omitted_when_empty(self):
        from poob.discord_bot.formatter import format_deal_embed

        listing = Listing(title="Item", price=10.0)
        deal = Deal(listing_id="l1", score=DealScore.FAIR, provenance_json="")
        embed = format_deal_embed(deal, listing)
        field_names = [f.name for f in embed.fields]
        assert "Evaluation Details" not in field_names

    def test_provenance_shows_score_adjustments(self):
        from poob.discord_bot.formatter import _format_provenance_field
        from poob.skills.models import DealProvenance

        prov = DealProvenance(
            score_adjustments=["incredible->great: $28 saved, 55%"],
            final_score="great",
        )
        deal = Deal(provenance_json=prov.to_json())
        text = _format_provenance_field(deal)
        assert text is not None
        assert "incredible->great" in text

    def test_provenance_roundtrip(self):
        """DealProvenance should survive JSON round-trip."""
        from poob.skills.models import DealProvenance

        prov = DealProvenance(
            vlm_providers=["gemini_flash"],
            vlm_agreement=0.7,
            scam_signals=["suspicious pricing"],
            score_adjustments=["good->fair: $3 saved, 15%"],
            web_search_used=True,
        )
        restored = DealProvenance.from_json(prov.to_json())
        assert restored.vlm_providers == ["gemini_flash"]
        assert restored.vlm_agreement == 0.7
        assert restored.scam_signals == ["suspicious pricing"]
        assert restored.web_search_used is True

    def test_provenance_from_empty_json(self):
        from poob.skills.models import DealProvenance

        p = DealProvenance.from_json("")
        assert p.vlm_providers == []
        assert p.final_score == ""

    def test_provenance_ignores_unknown_keys(self):
        from poob.skills.models import DealProvenance

        p = DealProvenance.from_json('{"unknown_field": 123, "final_score": "great"}')
        assert p.final_score == "great"
        assert not hasattr(p, "unknown_field")


class TestFormatListingEmbed:
    """Tests for format_listing_embed()."""

    def test_listing_embed_has_title_and_price(self):
        """Listing embed should show title and price."""
        from poob.discord_bot.formatter import format_listing_embed

        listing = Listing(
            title="Mountain Bike",
            price=350.0,
            location="Seattle, WA",
        )
        embed = format_listing_embed(listing)
        assert isinstance(embed, discord.Embed)
        assert "Mountain Bike" in embed.title
        field_names = [f.name for f in embed.fields]
        assert "Price" in field_names


class TestFormatWatchlistEmbed:
    """Tests for format_watchlist_embed()."""

    def test_watchlist_embed_lists_items(self):
        """Watchlist embed should list all watch items."""
        from poob.discord_bot.formatter import format_watchlist_embed

        watches = [
            WatchItem(id="w1", interest="PS5", max_price=300.0),
            WatchItem(id="w2", interest="Xbox", max_price=400.0),
        ]
        embed = format_watchlist_embed(watches)
        assert isinstance(embed, discord.Embed)
        assert len(embed.fields) == 2

    def test_watchlist_embed_empty(self):
        """Empty watchlist should produce an embed with no fields."""
        from poob.discord_bot.formatter import format_watchlist_embed

        embed = format_watchlist_embed([])
        assert isinstance(embed, discord.Embed)
        assert "no active" in embed.description.lower() or len(embed.fields) == 0


class TestFormatStatusEmbed:
    """Tests for format_status_embed()."""

    def test_status_embed_shows_state(self):
        """Status embed should show running/paused state."""
        from poob.discord_bot.formatter import format_status_embed

        embed = format_status_embed(
            is_running=True,
            is_paused=False,
            last_scan_time=None,
            next_scan_time=None,
            registered_sites=["facebook_marketplace"],
        )
        assert isinstance(embed, discord.Embed)
        field_names = [f.name for f in embed.fields]
        assert "Status" in field_names
        assert "Sites" in field_names
