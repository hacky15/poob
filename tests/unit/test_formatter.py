"""Tests for Discord embed formatters."""

from __future__ import annotations

import discord
import pytest

from agentic_scraper.storage.models import Deal, DealScore, Listing, WatchItem


class TestFormatDealEmbed:
    """Tests for format_deal_embed()."""

    def test_deal_embed_has_title_and_fields(self):
        """Deal embed should contain the listing title and key fields."""
        from agentic_scraper.discord_bot.formatter import format_deal_embed

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
        from agentic_scraper.discord_bot.formatter import format_deal_embed, SCORE_COLORS

        listing = Listing(title="Item", price=10.0)
        for score, expected_color in SCORE_COLORS.items():
            deal = Deal(listing_id="l1", score=score)
            embed = format_deal_embed(deal, listing)
            assert embed.color.value == expected_color.value

    def test_deal_embed_includes_link(self):
        """Deal embed should link to the listing."""
        from agentic_scraper.discord_bot.formatter import format_deal_embed

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
        from agentic_scraper.discord_bot.formatter import format_deal_embed

        listing = Listing(title="Item", price=10.0, image_urls=[])
        deal = Deal(listing_id="l1", score=DealScore.FAIR)
        embed = format_deal_embed(deal, listing)
        # No image should be set when listing has no images
        assert embed.image.url is None or embed.image.url == ""


class TestFormatListingEmbed:
    """Tests for format_listing_embed()."""

    def test_listing_embed_has_title_and_price(self):
        """Listing embed should show title and price."""
        from agentic_scraper.discord_bot.formatter import format_listing_embed

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
        from agentic_scraper.discord_bot.formatter import format_watchlist_embed

        watches = [
            WatchItem(id="w1", keywords="PS5", max_price=300.0),
            WatchItem(id="w2", keywords="Xbox", max_price=400.0),
        ]
        embed = format_watchlist_embed(watches)
        assert isinstance(embed, discord.Embed)
        assert len(embed.fields) == 2

    def test_watchlist_embed_empty(self):
        """Empty watchlist should produce an embed with no fields."""
        from agentic_scraper.discord_bot.formatter import format_watchlist_embed

        embed = format_watchlist_embed([])
        assert isinstance(embed, discord.Embed)
        assert "no active" in embed.description.lower() or len(embed.fields) == 0


class TestFormatStatusEmbed:
    """Tests for format_status_embed()."""

    def test_status_embed_shows_state(self):
        """Status embed should show running/paused state."""
        from agentic_scraper.discord_bot.formatter import format_status_embed

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
