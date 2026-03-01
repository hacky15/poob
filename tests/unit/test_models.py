"""Tests for data models: Listing, WatchItem, Deal, ScanLog, DealScore."""

from datetime import datetime, timezone

import pytest

from agentic_scraper.storage.models import Deal, DealScore, Listing, ScanLog, WatchItem


class TestDealScore:
    """DealScore enum values and ordering."""

    def test_enum_values(self):
        assert DealScore.UNKNOWN.value == "unknown"
        assert DealScore.FAIR.value == "fair"
        assert DealScore.GOOD.value == "good"
        assert DealScore.GREAT.value == "great"
        assert DealScore.INCREDIBLE.value == "incredible"

    def test_from_string(self):
        assert DealScore("good") == DealScore.GOOD
        assert DealScore("incredible") == DealScore.INCREDIBLE

    def test_invalid_value_raises(self):
        with pytest.raises(ValueError):
            DealScore("amazing")


class TestListing:
    """Listing dataclass creation and defaults."""

    def test_create_with_required_fields(self):
        listing = Listing(
            site="facebook_marketplace",
            external_id="fb_123",
            title="Test Item",
            price=50.00,
        )
        assert listing.site == "facebook_marketplace"
        assert listing.external_id == "fb_123"
        assert listing.title == "Test Item"
        assert listing.price == 50.00

    def test_default_values(self):
        listing = Listing()
        assert listing.id is None
        assert listing.site == ""
        assert listing.external_id == ""
        assert listing.title == ""
        assert listing.price is None
        assert listing.currency == "USD"
        assert listing.description == ""
        assert listing.location == ""
        assert listing.seller_name == ""
        assert listing.image_urls == []
        assert listing.listing_url == ""
        assert listing.posted_at is None
        assert listing.raw_data == {}

    def test_scraped_at_auto_set(self):
        listing = Listing()
        assert isinstance(listing.scraped_at, datetime)

    def test_image_urls_independent(self):
        """Each listing gets its own image_urls list (no shared mutable default)."""
        a = Listing()
        b = Listing()
        a.image_urls.append("test.jpg")
        assert b.image_urls == []

    def test_raw_data_independent(self):
        """Each listing gets its own raw_data dict."""
        a = Listing()
        b = Listing()
        a.raw_data["key"] = "value"
        assert b.raw_data == {}

    def test_full_listing(self, sample_listing):
        assert sample_listing.site == "facebook_marketplace"
        assert sample_listing.price == 250.00
        assert len(sample_listing.image_urls) == 1


class TestWatchItem:
    """WatchItem dataclass creation and defaults."""

    def test_create_minimal(self):
        item = WatchItem(interest="PS5")
        assert item.interest == "PS5"
        assert item.max_price is None
        assert item.is_active is True
        assert item.sites == []

    def test_create_full(self, sample_watch_item):
        assert sample_watch_item.interest == "PS5"
        assert sample_watch_item.max_price == 300.00
        assert sample_watch_item.location == "Portland, OR"
        assert sample_watch_item.radius_miles == 25
        assert sample_watch_item.discord_user_id == "user_001"

    def test_default_is_active(self):
        item = WatchItem()
        assert item.is_active is True

    def test_created_at_auto_set(self):
        item = WatchItem()
        assert isinstance(item.created_at, datetime)

    def test_sites_list_independent(self):
        a = WatchItem()
        b = WatchItem()
        a.sites.append("facebook")
        assert b.sites == []


class TestDeal:
    """Deal dataclass creation and defaults."""

    def test_create_minimal(self):
        deal = Deal(listing_id="abc")
        assert deal.listing_id == "abc"
        assert deal.watch_item_id is None
        assert deal.score == DealScore.UNKNOWN
        assert deal.notified is False

    def test_create_full(self, sample_deal):
        assert sample_deal.score == DealScore.GREAT
        assert sample_deal.estimated_market_price == 400.00
        assert sample_deal.discount_pct == 37.5
        assert "PS5" in sample_deal.llm_reasoning

    def test_notified_defaults_false(self):
        deal = Deal()
        assert deal.notified is False
        assert deal.notified_at is None


class TestScanLog:
    """ScanLog dataclass creation and defaults."""

    def test_create_minimal(self):
        log = ScanLog(site="facebook_marketplace", category="electronics")
        assert log.site == "facebook_marketplace"
        assert log.listings_found == 0
        assert log.deals_found == 0

    def test_defaults(self):
        log = ScanLog()
        assert log.errors == []
        assert log.duration_seconds == 0.0
        assert log.completed_at is None
        assert isinstance(log.started_at, datetime)

    def test_errors_list_independent(self):
        a = ScanLog()
        b = ScanLog()
        a.errors.append("timeout")
        assert b.errors == []
