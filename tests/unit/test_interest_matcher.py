"""Tests for InterestMatcher - matching listings against user interests."""

from __future__ import annotations

import pytest

from poob.scanner.interest_matcher import InterestMatcher, _score_from_discount
from poob.storage.models import Deal, DealScore, Listing, WatchItem


@pytest.fixture
def matcher() -> InterestMatcher:
    return InterestMatcher()


@pytest.fixture
def ps5_listing() -> Listing:
    return Listing(
        id="listing-1",
        site="facebook_marketplace",
        external_id="111",
        title="PS5 Console Bundle",
        price=300.0,
        listing_url="https://facebook.com/marketplace/item/111",
    )


@pytest.fixture
def iphone_listing() -> Listing:
    return Listing(
        id="listing-2",
        site="facebook_marketplace",
        external_id="222",
        title="iPhone 15 Pro Max 256GB",
        price=600.0,
        listing_url="https://facebook.com/marketplace/item/222",
    )


@pytest.fixture
def free_listing() -> Listing:
    return Listing(
        id="listing-3",
        site="facebook_marketplace",
        external_id="333",
        title="Free couch - must pick up",
        price=0.0,
        listing_url="https://facebook.com/marketplace/item/333",
    )


@pytest.fixture
def ps5_interest() -> WatchItem:
    return WatchItem(id="watch-1", interest="PS5", max_price=500.0)


@pytest.fixture
def iphone_interest() -> WatchItem:
    return WatchItem(id="watch-2", interest="iPhone 15 Pro", max_price=800.0)


@pytest.fixture
def couch_interest() -> WatchItem:
    return WatchItem(id="watch-3", interest="couch", max_price=100.0)


# --- _score_from_discount ---


class TestScoreFromDiscount:
    def test_incredible_at_60_percent(self):
        assert _score_from_discount(60.0) == DealScore.INCREDIBLE

    def test_incredible_above_60(self):
        assert _score_from_discount(80.0) == DealScore.INCREDIBLE

    def test_great_at_40_percent(self):
        assert _score_from_discount(40.0) == DealScore.GREAT

    def test_great_between_40_and_60(self):
        assert _score_from_discount(55.0) == DealScore.GREAT

    def test_good_at_20_percent(self):
        assert _score_from_discount(20.0) == DealScore.GOOD

    def test_good_between_20_and_40(self):
        assert _score_from_discount(35.0) == DealScore.GOOD

    def test_fair_above_zero(self):
        assert _score_from_discount(10.0) == DealScore.FAIR

    def test_unknown_at_zero(self):
        assert _score_from_discount(0.0) == DealScore.UNKNOWN

    def test_unknown_negative(self):
        assert _score_from_discount(-5.0) == DealScore.UNKNOWN


# --- InterestMatcher._interest_matches ---


class TestInterestMatches:
    def test_single_word_match(self):
        assert InterestMatcher._interest_matches("PS5 Console Bundle", "PS5") is True

    def test_multi_word_match(self):
        assert InterestMatcher._interest_matches("iPhone 15 Pro Max 256GB", "iPhone 15 Pro") is True

    def test_case_insensitive(self):
        assert InterestMatcher._interest_matches("ps5 console bundle", "PS5") is True

    def test_no_match(self):
        assert InterestMatcher._interest_matches("Xbox Series X", "PS5") is False

    def test_partial_word_match(self):
        """'couch' should match 'Free couch - must pick up'."""
        assert InterestMatcher._interest_matches("Free couch - must pick up", "couch") is True

    def test_empty_interest_returns_false(self):
        assert InterestMatcher._interest_matches("PS5 Console", "") is False

    def test_all_words_must_match(self):
        """All words in the interest must appear in the title."""
        assert InterestMatcher._interest_matches("iPhone 15 256GB", "iPhone 15 Pro") is False

    # --- Synonym-based fuzzy matching ---

    def test_synonym_ps5_matches_playstation5(self):
        """Interest 'PS5' should match title containing 'PlayStation 5'."""
        assert InterestMatcher._interest_matches("PlayStation 5 Disc Edition", "PS5") is True

    def test_synonym_playstation5_matches_ps5(self):
        """Interest 'PlayStation 5' should match title containing 'PS5'."""
        assert InterestMatcher._interest_matches("PS5 Bundle with Games", "playstation 5") is True

    def test_synonym_fridge_matches_refrigerator(self):
        """Interest 'fridge' should match 'Samsung Refrigerator'."""
        assert InterestMatcher._interest_matches("Samsung Refrigerator Stainless", "fridge") is True

    def test_synonym_refrigerator_matches_fridge(self):
        """Interest 'refrigerator' should match 'Mini Fridge'."""
        assert InterestMatcher._interest_matches("Mini Fridge Great Condition", "refrigerator") is True

    def test_synonym_tv_matches_television(self):
        """Interest 'TV' should match '55 inch Television'."""
        assert InterestMatcher._interest_matches("55 inch Television Samsung", "TV") is True

    def test_synonym_couch_matches_sofa(self):
        """Interest 'couch' should match 'Leather Sofa'."""
        assert InterestMatcher._interest_matches("Leather Sofa Good Condition", "couch") is True

    def test_synonym_gpu_matches_graphics_card(self):
        """Interest 'GPU' should match 'Graphics Card RTX 4070'."""
        assert InterestMatcher._interest_matches("NVIDIA Graphics Card RTX 4070", "GPU") is True

    def test_synonym_bike_matches_bicycle(self):
        """Interest 'bike' should match 'Mountain Bicycle'."""
        assert InterestMatcher._interest_matches("Mountain Bicycle Trek", "bike") is True

    def test_no_false_positives_from_synonyms(self):
        """Synonyms should not create false matches. 'PS4' should not match 'PS5'."""
        assert InterestMatcher._interest_matches("PS5 Disc Edition", "PS4") is False

    def test_exact_match_still_takes_priority(self):
        """Exact word matching should still work without needing synonyms."""
        assert InterestMatcher._interest_matches("PS5 Console Bundle", "PS5") is True

    def test_synonym_washer_matches_washing_machine(self):
        """Multi-word synonym: 'washer' should match 'Washing Machine'."""
        assert InterestMatcher._interest_matches("LG Washing Machine", "washer") is True


# --- InterestMatcher._price_within_budget ---


class TestPriceWithinBudget:
    def test_price_under_budget(self):
        assert InterestMatcher._price_within_budget(300.0, 500.0) is True

    def test_price_at_budget(self):
        assert InterestMatcher._price_within_budget(500.0, 500.0) is True

    def test_price_over_budget(self):
        assert InterestMatcher._price_within_budget(600.0, 500.0) is False

    def test_no_max_price_always_matches(self):
        assert InterestMatcher._price_within_budget(99999.0, None) is True

    def test_no_listing_price_always_matches(self):
        assert InterestMatcher._price_within_budget(None, 500.0) is True

    def test_both_none_matches(self):
        assert InterestMatcher._price_within_budget(None, None) is True


# --- InterestMatcher._calculate_discount ---


class TestCalculateDiscount:
    def test_40_percent_discount(self):
        assert InterestMatcher._calculate_discount(300.0, 500.0) == pytest.approx(40.0)

    def test_zero_discount_at_max(self):
        assert InterestMatcher._calculate_discount(500.0, 500.0) == pytest.approx(0.0)

    def test_100_percent_discount_free(self):
        assert InterestMatcher._calculate_discount(0.0, 100.0) == pytest.approx(100.0)

    def test_none_price_returns_zero(self):
        assert InterestMatcher._calculate_discount(None, 500.0) == 0.0

    def test_none_max_price_returns_zero(self):
        assert InterestMatcher._calculate_discount(300.0, None) == 0.0

    def test_zero_max_price_returns_zero(self):
        assert InterestMatcher._calculate_discount(300.0, 0.0) == 0.0


# --- InterestMatcher.match ---


class TestMatch:
    def test_matching_listing_and_interest(self, matcher, ps5_listing, ps5_interest):
        deals = matcher.match([ps5_listing], [ps5_interest])
        assert len(deals) == 1
        assert deals[0].listing_id == "listing-1"
        assert deals[0].watch_item_id == "watch-1"
        assert deals[0].score == DealScore.GREAT  # 40% off

    def test_no_match_wrong_keywords(self, matcher, ps5_listing, iphone_interest):
        deals = matcher.match([ps5_listing], [iphone_interest])
        assert len(deals) == 0

    def test_no_match_over_budget(self, matcher, iphone_listing):
        cheap_interest = WatchItem(id="w1", interest="iPhone 15 Pro", max_price=400.0)
        deals = matcher.match([iphone_listing], [cheap_interest])
        assert len(deals) == 0

    def test_multiple_interests_match_same_listing(self, matcher, ps5_listing):
        interest1 = WatchItem(id="w1", interest="PS5", max_price=500.0)
        interest2 = WatchItem(id="w2", interest="PS5 Console", max_price=400.0)
        deals = matcher.match([ps5_listing], [interest1, interest2])
        assert len(deals) == 2

    def test_multiple_listings_multiple_interests(
        self, matcher, ps5_listing, iphone_listing, ps5_interest, iphone_interest
    ):
        deals = matcher.match(
            [ps5_listing, iphone_listing], [ps5_interest, iphone_interest]
        )
        assert len(deals) == 2

    def test_empty_listings(self, matcher, ps5_interest):
        deals = matcher.match([], [ps5_interest])
        assert deals == []

    def test_empty_interests(self, matcher, ps5_listing):
        deals = matcher.match([ps5_listing], [])
        assert deals == []

    def test_deal_has_correct_discount(self, matcher, ps5_listing, ps5_interest):
        deals = matcher.match([ps5_listing], [ps5_interest])
        assert deals[0].discount_pct == pytest.approx(40.0)

    def test_deal_has_reasoning(self, matcher, ps5_listing, ps5_interest):
        deals = matcher.match([ps5_listing], [ps5_interest])
        assert "PS5" in deals[0].llm_reasoning

    def test_free_listing_matches_interest(self, matcher, free_listing, couch_interest):
        deals = matcher.match([free_listing], [couch_interest])
        assert len(deals) == 1
        assert deals[0].score == DealScore.INCREDIBLE  # 100% off


# --- InterestMatcher.match_single ---


class TestMatchSingle:
    def test_delegates_to_match(self, matcher, ps5_listing, ps5_interest):
        deals = matcher.match_single(ps5_listing, [ps5_interest])
        assert len(deals) == 1
        assert deals[0].listing_id == "listing-1"

    def test_no_matches(self, matcher, ps5_listing, iphone_interest):
        deals = matcher.match_single(ps5_listing, [iphone_interest])
        assert len(deals) == 0

