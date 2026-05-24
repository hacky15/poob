"""Unit tests for Facebook Marketplace parser."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from poob.sites.facebook.parser import (
    _merge_marketplace_urls,
    _parse_csv_listings,
    _parse_freshness,
    _parse_json_robust,
    _parse_text_listings,
    parse_listings,
)


class TestParseFreshness:
    """Coverage for every Facebook freshness-badge format we've observed.

    Each "fresh" pattern MUST resolve to a posted_at within a few minutes
    of now so it passes the 10-minute public notification gate. Each
    "stale" pattern MUST resolve to something older than that gate.
    """

    def _now(self) -> datetime:
        return datetime.now(timezone.utc)

    @pytest.mark.parametrize("text", [
        "Just listed",
        "Just posted",
        "just listed in Madison",
        "JUST LISTED",
    ])
    def test_just_listed_variants(self, text: str) -> None:
        result = _parse_freshness(text)
        assert result is not None
        assert (self._now() - result).total_seconds() < 60

    @pytest.mark.parametrize("text,expected_min", [
        ("a minute ago", 1),
        ("a few minutes ago", 3),
        ("Listed a few minutes ago", 3),
        ("3 minutes ago", 3),
        ("Listed 5 minutes ago", 5),
        ("Posted 12 minutes ago", 12),
        ("Updated 7 minutes ago", 7),
        ("5m ago", 5),
        ("12m ago", 12),
        ("3 mins ago", 3),
    ])
    def test_minutes_ago_variants(self, text: str, expected_min: int) -> None:
        result = _parse_freshness(text)
        assert result is not None
        delta = (self._now() - result).total_seconds() / 60
        # ±1 minute tolerance for now() drift between call and assertion.
        assert abs(delta - expected_min) <= 1

    @pytest.mark.parametrize("text,expected_h", [
        ("an hour ago", 1),
        ("about an hour ago", 1),
        ("Listed an hour ago", 1),
        ("2 hours ago", 2),
        ("Listed 5 hours ago", 5),
        ("Updated 3 hours ago", 3),
        ("5h ago", 5),
        ("2 hrs ago", 2),
    ])
    def test_hours_ago_variants(self, text: str, expected_h: int) -> None:
        result = _parse_freshness(text)
        assert result is not None
        delta_h = (self._now() - result).total_seconds() / 3600
        assert abs(delta_h - expected_h) < 0.1

    @pytest.mark.parametrize("text", [
        "yesterday",
        "Listed yesterday",
        "yesterday at 3:14 PM",
        "Posted yesterday",
    ])
    def test_yesterday_variants(self, text: str) -> None:
        result = _parse_freshness(text)
        assert result is not None
        delta_h = (self._now() - result).total_seconds() / 3600
        assert abs(delta_h - 24) < 0.1

    @pytest.mark.parametrize("text,expected_d", [
        ("3 days ago", 3),
        ("Listed 5 days ago", 5),
        ("3d ago", 3),
        ("last week", 7),
        ("Listed last week", 7),
        ("2 weeks ago", 14),
        ("Listed 3 weeks ago", 21),
        ("2w ago", 14),
        ("Listed 2 months ago", 60),
        ("3 mo ago", 90),
    ])
    def test_older_variants(self, text: str, expected_d: int) -> None:
        result = _parse_freshness(text)
        assert result is not None
        delta_d = (self._now() - result).total_seconds() / 86400
        assert abs(delta_d - expected_d) < 0.5

    def test_unparseable_returns_none(self) -> None:
        assert _parse_freshness("hello world") is None
        assert _parse_freshness("") is None
        assert _parse_freshness("   ") is None
        # Just numbers without time unit
        assert _parse_freshness("5") is None

    def test_freshness_in_longer_text(self) -> None:
        """Real page-text use case: freshness embedded in a page dump."""
        haystack = (
            "Solid wood vintage dresser with mirror\n"
            "$120\n"
            "Listed 5 minutes ago in Appleton, WI\n"
            "Condition: Used - good\n"
        )
        result = _parse_freshness(haystack)
        assert result is not None
        delta_min = (self._now() - result).total_seconds() / 60
        assert abs(delta_min - 5) < 1


# ---------------------------------------------------------------------------
# _parse_csv_listings
# ---------------------------------------------------------------------------

class TestParseCsvListings:
    """Tests for the CSV/dash-separated listing parser."""

    def test_basic_dash_format(self):
        text = (
            "- Black Coffee Table, $60, Green Bay, WI\n"
            "- Coffee Table, $45, Appleton, WI\n"
        )
        result = _parse_csv_listings(text)
        assert result is not None
        assert len(result) == 2
        assert result[0]["title"] == "Black Coffee Table"
        assert result[0]["price"] == "$60"
        assert result[0]["location"] == "Green Bay, WI"
        assert result[1]["title"] == "Coffee Table"
        assert result[1]["price"] == "$45"
        assert result[1]["location"] == "Appleton, WI"

    def test_price_range_with_en_dash(self):
        text = "- Coffee Table, $45\u2013$50, Green Bay, WI\n"
        result = _parse_csv_listings(text)
        assert result is not None
        assert len(result) == 1
        assert result[0]["title"] == "Coffee Table"
        assert result[0]["price"] == "$45\u2013$50"

    def test_price_range_with_hyphen(self):
        text = "- Table, $45-$50, City, ST\n"
        result = _parse_csv_listings(text)
        assert result is not None
        assert result[0]["price"] == "$45-$50"

    def test_with_trailing_url_in_brackets(self):
        text = "- Coffee Table, $60, Portland, OR [https://www.facebook.com/marketplace/item/123456]\n"
        result = _parse_csv_listings(text)
        assert result is not None
        assert result[0]["listing_url"] == "https://www.facebook.com/marketplace/item/123456"
        assert result[0]["external_id"] == "123456"

    def test_with_trailing_bare_url(self):
        text = "- Coffee Table, $60, Portland, OR https://www.facebook.com/marketplace/item/789\n"
        result = _parse_csv_listings(text)
        assert result is not None
        assert result[0]["listing_url"] == "https://www.facebook.com/marketplace/item/789"

    def test_asterisk_bullets(self):
        text = "* Side Table, $30, Salem, OR\n"
        result = _parse_csv_listings(text)
        assert result is not None
        assert result[0]["title"] == "Side Table"

    def test_no_price_still_parsed(self):
        text = "- Free Coffee Table, Green Bay, WI\n"
        result = _parse_csv_listings(text)
        assert result is not None
        assert result[0]["title"] == "Free Coffee Table, Green Bay, WI"
        assert result[0]["price"] == ""

    def test_non_bullet_lines_ignored(self):
        text = "Here are the listings:\n- Table, $50, City, ST\nEnd of results\n"
        result = _parse_csv_listings(text)
        assert result is not None
        assert len(result) == 1

    def test_empty_input(self):
        assert _parse_csv_listings("") is None
        assert _parse_csv_listings("no listings here") is None

    def test_comma_in_price(self):
        text = "- Fancy Table, $1,200, New York, NY\n"
        result = _parse_csv_listings(text)
        assert result is not None
        assert result[0]["price"] == "$1,200"

    def test_decimal_price(self):
        text = "- Table, $59.99, Portland, OR\n"
        result = _parse_csv_listings(text)
        assert result is not None
        assert result[0]["price"] == "$59.99"


# ---------------------------------------------------------------------------
# _merge_marketplace_urls
# ---------------------------------------------------------------------------

class TestMergeMarketplaceUrls:
    """Tests for URL merging from raw output."""

    def test_merges_urls_into_listings_without_urls(self):
        data = [
            {"title": "Table A", "price": "$50", "listing_url": ""},
            {"title": "Table B", "price": "$75", "listing_url": ""},
        ]
        raw = (
            "Listings:\n- Table A, $50\n- Table B, $75\n\n"
            "Links:\nhttps://www.facebook.com/marketplace/item/111/\n"
            "https://www.facebook.com/marketplace/item/222/\n"
        )
        _merge_marketplace_urls(data, raw)
        assert "/marketplace/item/111" in data[0]["listing_url"]
        assert data[0]["external_id"] == "111"
        assert "/marketplace/item/222" in data[1]["listing_url"]
        assert data[1]["external_id"] == "222"

    def test_skips_listings_that_already_have_urls(self):
        data = [
            {"title": "A", "listing_url": "https://www.facebook.com/marketplace/item/999"},
            {"title": "B", "listing_url": ""},
        ]
        raw = "https://www.facebook.com/marketplace/item/111/\n"
        _merge_marketplace_urls(data, raw)
        # First item kept its URL, second got the found URL
        assert data[0]["listing_url"] == "https://www.facebook.com/marketplace/item/999"
        assert "/marketplace/item/111" in data[1]["listing_url"]

    def test_deduplicates_urls(self):
        data = [
            {"title": "A", "listing_url": ""},
            {"title": "B", "listing_url": ""},
        ]
        raw = (
            "https://www.facebook.com/marketplace/item/111/\n"
            "https://www.facebook.com/marketplace/item/111/\n"  # duplicate
            "https://www.facebook.com/marketplace/item/222/\n"
        )
        _merge_marketplace_urls(data, raw)
        assert "/marketplace/item/111" in data[0]["listing_url"]
        assert "/marketplace/item/222" in data[1]["listing_url"]

    def test_no_urls_in_output(self):
        data = [{"title": "A", "listing_url": ""}]
        _merge_marketplace_urls(data, "no urls here")
        assert data[0]["listing_url"] == ""

    def test_more_listings_than_urls(self):
        data = [
            {"title": "A", "listing_url": ""},
            {"title": "B", "listing_url": ""},
            {"title": "C", "listing_url": ""},
        ]
        raw = "https://www.facebook.com/marketplace/item/111/\n"
        _merge_marketplace_urls(data, raw)
        assert "/marketplace/item/111" in data[0]["listing_url"]
        assert data[1]["listing_url"] == ""
        assert data[2]["listing_url"] == ""


# ---------------------------------------------------------------------------
# parse_listings (integration of all parsers)
# ---------------------------------------------------------------------------

class TestParseListingsIntegration:
    """Integration tests for parse_listings with different formats."""

    def test_csv_format_produces_listings(self):
        text = (
            "- Black Coffee Table, $60, Green Bay, WI\n"
            "- Coffee Table, $45, Appleton, WI\n"
        )
        listings = parse_listings(text, site="facebook_marketplace")
        assert len(listings) == 2
        assert listings[0].title == "Black Coffee Table"
        assert listings[0].price == 60.0
        assert listings[0].location == "Green Bay, WI"
        assert listings[0].site == "facebook_marketplace"

    def test_csv_with_urls_in_output(self):
        text = (
            "- Coffee Table, $60, Portland, OR\n\n"
            "Found links:\nhttps://www.facebook.com/marketplace/item/12345/\n"
        )
        listings = parse_listings(text, site="facebook_marketplace")
        assert len(listings) == 1
        assert "/marketplace/item/12345" in listings[0].listing_url
        assert listings[0].external_id == "12345"

    def test_price_range_parsed_to_first_price(self):
        text = "- Table, $45\u2013$50, City, ST\n"
        listings = parse_listings(text, site="facebook_marketplace")
        assert len(listings) == 1
        assert listings[0].price == 45.0  # takes the first price

    def test_json_format_still_works(self):
        text = '[{"title": "Desk", "price": 100, "location": "Portland"}]'
        listings = parse_listings(text, site="facebook_marketplace")
        assert len(listings) == 1
        assert listings[0].title == "Desk"
        assert listings[0].price == 100.0

    def test_numbered_text_format_still_works(self):
        text = (
            "1. Title: Desk\n"
            "   Price: $100\n"
            "   Location: Portland, OR\n"
            "   Listing_url: /marketplace/item/555/\n"
        )
        listings = parse_listings(text, site="facebook_marketplace")
        assert len(listings) == 1
        assert listings[0].title == "Desk"
        assert listings[0].listing_url == "https://www.facebook.com/marketplace/item/555/"

    def test_empty_input(self):
        assert parse_listings("", site="test") == []
        assert parse_listings("   ", site="test") == []
        assert parse_listings(None, site="test") == []

    def test_unparseable_input(self):
        assert parse_listings("random text with no structure", site="test") == []


# ---------------------------------------------------------------------------
# Embedded price rescue from title
# ---------------------------------------------------------------------------

class TestEmbeddedPriceRescue:
    """Tests for extracting embedded prices from concatenated title text."""

    def test_just_listed_with_embedded_price(self):
        """FB concatenates 'Just listed$200Title' — should rescue price."""
        from poob.sites.facebook.parser import _dict_to_listing

        listing = _dict_to_listing(
            {"title": "Just listed$200Haaka 30 pound sausage stuffer", "price": None},
            site="facebook_marketplace",
        )
        assert listing.price == 200.0
        assert "Haaka" in listing.title
        assert "$200" not in listing.title
        assert "Just listed" not in listing.title

    def test_embedded_price_with_comma(self):
        """Should handle $1,500 embedded in title."""
        from poob.sites.facebook.parser import _dict_to_listing

        listing = _dict_to_listing(
            {"title": "$1,500Samsung 65 inch TV", "price": None},
            site="facebook_marketplace",
        )
        assert listing.price == 1500.0
        assert "Samsung" in listing.title

    def test_no_embedded_price_leaves_title_alone(self):
        """Title without $ should not be modified."""
        from poob.sites.facebook.parser import _dict_to_listing

        listing = _dict_to_listing(
            {"title": "Free couch good condition", "price": None},
            site="facebook_marketplace",
        )
        assert listing.price is None
        assert listing.title == "Free couch good condition"

    def test_normal_price_not_modified(self):
        """When price is already set, title should not be touched."""
        from poob.sites.facebook.parser import _dict_to_listing

        listing = _dict_to_listing(
            {"title": "PS5 Console", "price": 350.0},
            site="facebook_marketplace",
        )
        assert listing.price == 350.0
        assert listing.title == "PS5 Console"

    def test_listed_ago_prefix_stripped(self):
        """Should strip 'Listed 2h ago' prefix along with price."""
        from poob.sites.facebook.parser import _dict_to_listing

        listing = _dict_to_listing(
            {"title": "Listed 2h ago$50Nice lamp", "price": None},
            site="facebook_marketplace",
        )
        assert listing.price == 50.0
        assert "lamp" in listing.title.lower()
