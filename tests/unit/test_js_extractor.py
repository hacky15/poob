"""Unit tests for Facebook Marketplace JavaScript extraction scripts."""

from __future__ import annotations

from agentic_scraper.sites.facebook.js_extractor import (
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
