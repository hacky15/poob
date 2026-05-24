"""Unit tests for Facebook Marketplace JavaScript extraction scripts."""

from __future__ import annotations

from poob.sites.facebook.js_extractor import (
    EXTRACT_LISTINGS_JS,
    EXTRACT_LISTING_URLS_JS,
    SCROLL_DOWN_JS,
)


class TestJsExtractorConstants:
    """Validate JS extraction constants are well-formed."""

    def test_extract_listings_js_is_arrow_function(self):
        """EXTRACT_LISTINGS_JS must be a valid arrow function for page.evaluate()."""
        stripped = EXTRACT_LISTINGS_JS.strip()
        assert stripped.startswith("("), f"Must start with '(': {stripped[:50]}"
        assert "=>" in stripped, "Must contain '=>'"

    def test_extract_listing_urls_js_is_arrow_function(self):
        """EXTRACT_LISTING_URLS_JS must be a valid arrow function."""
        stripped = EXTRACT_LISTING_URLS_JS.strip()
        assert stripped.startswith("("), f"Must start with '(': {stripped[:50]}"
        assert "=>" in stripped, "Must contain '=>'"

    def test_scroll_down_js_is_arrow_function(self):
        """SCROLL_DOWN_JS must be a valid arrow function."""
        stripped = SCROLL_DOWN_JS.strip()
        assert stripped.startswith("("), f"Must start with '(': {stripped[:50]}"
        assert "=>" in stripped, "Must contain '=>'"

    def test_all_constants_are_non_empty(self):
        """All JS constants must be non-empty strings."""
        assert len(EXTRACT_LISTINGS_JS.strip()) > 50
        assert len(EXTRACT_LISTING_URLS_JS.strip()) > 20
        assert len(SCROLL_DOWN_JS.strip()) > 10

    def test_extract_listings_targets_marketplace_items(self):
        """EXTRACT_LISTINGS_JS should target marketplace item links."""
        assert "/marketplace/item/" in EXTRACT_LISTINGS_JS

    def test_extract_listing_urls_targets_marketplace_items(self):
        """EXTRACT_LISTING_URLS_JS should target marketplace item links."""
        assert "/marketplace/item/" in EXTRACT_LISTING_URLS_JS


class TestFreshnessRegexBroadened:
    """The JS-side freshness regex must accept every badge format the
    Python parser accepts — narrower JS = silent timestamp drop."""

    def test_handles_just_listed(self):
        assert "Just (?:listed|posted)" in EXTRACT_LISTINGS_JS

    def test_handles_posted_prefix(self):
        assert "Posted" in EXTRACT_LISTINGS_JS

    def test_handles_updated_prefix(self):
        assert "Updated" in EXTRACT_LISTINGS_JS

    def test_handles_word_form_quantifiers(self):
        # "a few minutes ago" / "an hour ago" / "a minute ago"
        assert "an?" in EXTRACT_LISTINGS_JS
        assert "few" in EXTRACT_LISTINGS_JS

    def test_handles_about_prefix(self):
        # "about an hour ago"
        assert "about" in EXTRACT_LISTINGS_JS

    def test_handles_abbreviated_units(self):
        # "5m ago", "3h ago", "2d ago", "2w ago"
        assert "mhdwy" in EXTRACT_LISTINGS_JS

    def test_handles_month_units(self):
        # "3 months ago" / "3mo ago"
        assert "months?|mos?" in EXTRACT_LISTINGS_JS

    def test_handles_year_units(self):
        assert "years?|yr" in EXTRACT_LISTINGS_JS

    def test_handles_yesterday(self):
        assert "yesterday" in EXTRACT_LISTINGS_JS

    def test_handles_last_week(self):
        assert "last\\s+week" in EXTRACT_LISTINGS_JS
