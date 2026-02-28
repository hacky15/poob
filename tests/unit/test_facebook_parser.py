"""Unit tests for Facebook Marketplace parser."""

from __future__ import annotations

import pytest

from agentic_scraper.sites.facebook.parser import (
    _merge_marketplace_urls,
    _parse_csv_listings,
    _parse_json_robust,
    _parse_text_listings,
    parse_listings,
)


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
