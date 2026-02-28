"""Tests for Facebook Marketplace category mapping and relevance filtering."""

from __future__ import annotations

import pytest

from agentic_scraper.sites.facebook.categories import (
    get_category_slug,
    infer_category,
    is_relevant_to_category,
)


# --- get_category_slug tests ---


class TestGetCategorySlug:
    """Tests for category name -> Facebook URL slug mapping."""

    def test_furniture_maps_to_furniture(self):
        assert get_category_slug("furniture") == "furniture"

    def test_electronics_maps_to_electronics(self):
        assert get_category_slug("electronics") == "electronics"

    def test_clothing_maps_to_apparel(self):
        assert get_category_slug("clothing") == "apparel"

    def test_case_insensitive(self):
        assert get_category_slug("Furniture") == "furniture"
        assert get_category_slug("ELECTRONICS") == "electronics"

    def test_strips_whitespace(self):
        assert get_category_slug("  furniture  ") == "furniture"

    def test_unknown_category_returns_none(self):
        assert get_category_slug("unknown_thing") is None

    def test_none_returns_none(self):
        assert get_category_slug(None) is None

    def test_empty_string_returns_none(self):
        assert get_category_slug("") is None

    def test_sporting_goods_variants(self):
        assert get_category_slug("sporting goods") == "sporting-goods"
        assert get_category_slug("sporting-goods") == "sporting-goods"
        assert get_category_slug("sports") == "sporting-goods"

    def test_appliances(self):
        assert get_category_slug("appliances") == "appliances"


# --- is_relevant_to_category tests ---


class TestIsRelevantToCategory:
    """Tests for listing title relevance filtering."""

    def test_no_category_always_relevant(self):
        assert is_relevant_to_category("Coffee Table Book", None) is True

    def test_furniture_accepts_actual_table(self):
        assert is_relevant_to_category("Coffee Table - Solid Wood", "furniture") is True

    def test_furniture_rejects_coffee_table_book(self):
        assert is_relevant_to_category("Coffee Table Book", "furniture") is False

    def test_furniture_rejects_books_plural(self):
        assert is_relevant_to_category("Lot of 5 Coffee Table Books", "furniture") is False

    def test_furniture_accepts_bookshelf(self):
        """Bookshelf is furniture - 'book' exclusion should not apply."""
        assert is_relevant_to_category("Wooden Bookshelf", "furniture") is True

    def test_furniture_accepts_bookcase(self):
        """Bookcase is furniture - 'book' exclusion should not apply."""
        assert is_relevant_to_category("Antique Bookcase", "furniture") is True

    def test_furniture_rejects_coffee_mug(self):
        assert is_relevant_to_category("Thyme and Table Coffee Mug", "furniture") is False

    def test_furniture_rejects_barbie_replacement(self):
        assert is_relevant_to_category(
            "2018 Mattel Barbie Dream House Coffee Table", "furniture"
        ) is False

    def test_furniture_case_insensitive(self):
        assert is_relevant_to_category("COFFEE TABLE BOOK", "furniture") is False

    def test_unknown_category_no_exclusions(self):
        """Categories without exclusion rules should accept everything."""
        assert is_relevant_to_category("Anything goes", "vehicles") is True

    def test_empty_title_is_relevant(self):
        assert is_relevant_to_category("", "furniture") is True

    def test_real_furniture_listings_pass(self):
        """Actual furniture listings should all pass the filter."""
        titles = [
            "40\" Contemporary Metal Coffee Table - Coastal Oak",
            "Mid Century Modern Coffee Table",
            "Rustic Farmhouse Coffee Table with Storage",
            "Glass Top Coffee Table",
            "IKEA LACK Coffee Table",
        ]
        for title in titles:
            assert is_relevant_to_category(title, "furniture") is True, f"Failed: {title}"

    def test_book_listings_rejected_for_furniture(self):
        """Coffee table books should be rejected when searching furniture."""
        titles = [
            "The Canadian Rockies Coffee Table Book",
            "Frida Kahlo Her Universe Coffee Table Book",
            "MUSTANG coffee table book 30th anniversary edition",
            "Vintage Americana Coffee Table Book",
            "Style A to Zoe Fashion Style Coffee Table Book",
        ]
        for title in titles:
            assert is_relevant_to_category(title, "furniture") is False, f"Passed: {title}"


# --- infer_category tests ---


class TestInferCategory:
    """Tests for keyword-based category inference."""

    def test_coffee_table_infers_furniture(self):
        assert infer_category("coffee table") == "furniture"

    def test_dining_table_infers_furniture(self):
        assert infer_category("dining table") == "furniture"

    def test_espresso_machine_infers_appliances(self):
        assert infer_category("espresso machine") == "appliances"

    def test_coffee_maker_infers_appliances(self):
        assert infer_category("coffee maker") == "appliances"

    def test_ps5_infers_electronics(self):
        assert infer_category("PS5") == "electronics"

    def test_iphone_infers_electronics(self):
        assert infer_category("iPhone 15 Pro") == "electronics"

    def test_case_insensitive(self):
        assert infer_category("COFFEE TABLE") == "furniture"
        assert infer_category("Espresso Machine") == "appliances"

    def test_unknown_keywords_return_none(self):
        assert infer_category("random stuff") is None

    def test_empty_string_returns_none(self):
        assert infer_category("") is None

    def test_partial_match_works(self):
        """Keywords embedded in longer queries should still match."""
        assert infer_category("vintage coffee table wood") == "furniture"
        assert infer_category("breville espresso machine used") == "appliances"
