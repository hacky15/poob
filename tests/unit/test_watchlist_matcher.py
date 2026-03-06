"""Tests for InterestMatcher - interest matching and price filtering."""

from __future__ import annotations

import pytest

from agentic_scraper.skills.models import ItemIdentification
from agentic_scraper.storage.models import DealScore, Listing, WatchItem


class TestKeywordMatch:
    """Tests for keyword-based matching (fast pre-filter path)."""

    def test_interest_matches_title(self):
        from agentic_scraper.scanner.interest_matcher import InterestMatcher

        matcher = InterestMatcher()
        listing = Listing(title="PlayStation 5 Disc Edition", price=250.0, external_id="1")
        watch = WatchItem(id="w1", interest="PlayStation 5", max_price=300.0)

        deals = matcher.match([listing], [watch])
        assert len(deals) == 1
        assert deals[0].listing_id == listing.id or deals[0].watch_item_id == "w1"

    def test_interest_case_insensitive(self):
        from agentic_scraper.scanner.interest_matcher import InterestMatcher

        matcher = InterestMatcher()
        listing = Listing(title="PLAYSTATION 5", price=250.0, external_id="1")
        watch = WatchItem(id="w1", interest="playstation 5", max_price=300.0)

        deals = matcher.match([listing], [watch])
        assert len(deals) == 1

    def test_no_match_when_interest_differs(self):
        from agentic_scraper.scanner.interest_matcher import InterestMatcher

        matcher = InterestMatcher()
        listing = Listing(title="Xbox Series X", price=300.0, external_id="1")
        watch = WatchItem(id="w1", interest="PlayStation 5", max_price=400.0)

        deals = matcher.match([listing], [watch])
        assert len(deals) == 0

    def test_price_within_budget(self):
        from agentic_scraper.scanner.interest_matcher import InterestMatcher

        matcher = InterestMatcher()
        listing = Listing(title="PS5", price=250.0, external_id="1")
        watch = WatchItem(id="w1", interest="PS5", max_price=300.0)

        deals = matcher.match([listing], [watch])
        assert len(deals) == 1

    def test_price_over_budget_excluded(self):
        from agentic_scraper.scanner.interest_matcher import InterestMatcher

        matcher = InterestMatcher()
        listing = Listing(title="PS5", price=500.0, external_id="1")
        watch = WatchItem(id="w1", interest="PS5", max_price=300.0)

        deals = matcher.match([listing], [watch])
        assert len(deals) == 0

    def test_no_max_price_always_matches(self):
        from agentic_scraper.scanner.interest_matcher import InterestMatcher

        matcher = InterestMatcher()
        listing = Listing(title="PS5", price=999.0, external_id="1")
        watch = WatchItem(id="w1", interest="PS5", max_price=None)

        deals = matcher.match([listing], [watch])
        assert len(deals) == 1

    def test_calculates_discount_percentage(self):
        from agentic_scraper.scanner.interest_matcher import InterestMatcher

        matcher = InterestMatcher()
        listing = Listing(title="PS5", price=200.0, external_id="1")
        watch = WatchItem(id="w1", interest="PS5", max_price=400.0)

        deals = matcher.match([listing], [watch])
        assert len(deals) == 1
        # 200 is 50% of 400 → 50% discount
        assert deals[0].discount_pct == pytest.approx(50.0)

    def test_assigns_deal_score_great(self):
        from agentic_scraper.scanner.interest_matcher import InterestMatcher

        matcher = InterestMatcher()
        listing = Listing(title="PS5", price=200.0, external_id="1")
        watch = WatchItem(id="w1", interest="PS5", max_price=400.0)

        deals = matcher.match([listing], [watch])
        assert deals[0].score == DealScore.GREAT

    def test_assigns_deal_score_good(self):
        from agentic_scraper.scanner.interest_matcher import InterestMatcher

        matcher = InterestMatcher()
        listing = Listing(title="PS5", price=280.0, external_id="1")
        watch = WatchItem(id="w1", interest="PS5", max_price=400.0)

        deals = matcher.match([listing], [watch])
        # 280/400 = 30% discount → GOOD
        assert deals[0].score == DealScore.GOOD

    def test_keyword_match_is_permissive(self):
        """Keyword match intentionally allows false positives like 'coffee table book'.

        This is fine because keyword matching is only used for pre-filtering.
        The real matching decision happens in match_with_identification using the LLM.
        """
        from agentic_scraper.scanner.interest_matcher import InterestMatcher

        matcher = InterestMatcher()
        listing = Listing(title="Coffee Table Book", price=8.0, external_id="1")
        watch = WatchItem(id="w1", interest="coffee table", max_price=100.0)

        # Keyword match SHOULD match — it's intentionally permissive
        deals = matcher.match([listing], [watch])
        assert len(deals) == 1


class TestLLMInformedMatch:
    """Tests for match_with_identification — the primary matching strategy."""

    def test_coffee_table_book_rejected(self):
        """LLM identified 'coffee table book' — should NOT match 'coffee table'."""
        from agentic_scraper.scanner.interest_matcher import InterestMatcher

        matcher = InterestMatcher()
        listing = Listing(title="Coffee Table Book", price=8.0, external_id="1")
        watch = WatchItem(id="w1", interest="coffee table", max_price=100.0)

        identification = ItemIdentification(
            item_name="Tricia Guild on Color coffee table book",
            category="books/art",
            confidence=0.85,
        )

        deals = matcher.match_with_identification(listing, [watch], identification)
        assert len(deals) == 0

    def test_real_coffee_table_matches(self):
        """LLM identified actual coffee table — should match."""
        from agentic_scraper.scanner.interest_matcher import InterestMatcher

        matcher = InterestMatcher()
        listing = Listing(
            title="Mid Century Modern Coffee Table - Solid Wood", price=75.0, external_id="1"
        )
        watch = WatchItem(id="w1", interest="coffee table", max_price=100.0)

        identification = ItemIdentification(
            item_name="Mid Century Modern Coffee Table",
            category="furniture/table/coffee",
            confidence=0.9,
        )

        deals = matcher.match_with_identification(listing, [watch], identification)
        assert len(deals) == 1

    def test_espresso_machine_guide_rejected(self):
        """LLM identified a book about espresso machines — should NOT match."""
        from agentic_scraper.scanner.interest_matcher import InterestMatcher

        matcher = InterestMatcher()
        listing = Listing(
            title="Espresso Machine Guide Book", price=10.0, external_id="1"
        )
        watch = WatchItem(id="w1", interest="espresso machine", max_price=150.0)

        identification = ItemIdentification(
            item_name="Espresso Machine Guide Book",
            category="books/reference",
            confidence=0.9,
        )

        deals = matcher.match_with_identification(listing, [watch], identification)
        assert len(deals) == 0

    def test_real_espresso_machine_matches(self):
        """LLM identified real espresso machine — should match."""
        from agentic_scraper.scanner.interest_matcher import InterestMatcher

        matcher = InterestMatcher()
        listing = Listing(
            title="Francis Francis! Espresso Machine X5", price=80.0, external_id="1"
        )
        watch = WatchItem(id="w1", interest="espresso machine", max_price=150.0)

        identification = ItemIdentification(
            item_name="Francis Francis Espresso Machine X5",
            category="appliances/coffee/espresso",
            confidence=0.85,
        )

        deals = matcher.match_with_identification(listing, [watch], identification)
        assert len(deals) == 1

    def test_ps5_poster_rejected(self):
        """LLM identified PS5 poster, not a console."""
        from agentic_scraper.scanner.interest_matcher import InterestMatcher

        matcher = InterestMatcher()
        listing = Listing(title="PS5 Poster - Wall Art", price=15.0, external_id="1")
        watch = WatchItem(id="w1", interest="PS5", max_price=500.0)

        identification = ItemIdentification(
            item_name="PlayStation 5 Wall Poster",
            category="art/poster",
            confidence=0.9,
        )

        # item_name doesn't contain "ps5" as a standalone match,
        # and category "art/poster" doesn't match either
        deals = matcher.match_with_identification(listing, [watch], identification)
        assert len(deals) == 0

    def test_ps5_console_matches(self):
        """LLM identified PS5 console — should match."""
        from agentic_scraper.scanner.interest_matcher import InterestMatcher

        matcher = InterestMatcher()
        listing = Listing(title="PS5 Console Bundle", price=300.0, external_id="1")
        watch = WatchItem(id="w1", interest="PS5", max_price=500.0)

        identification = ItemIdentification(
            item_name="PS5 Console Bundle",
            category="electronics/gaming/console",
            confidence=0.9,
        )

        deals = matcher.match_with_identification(listing, [watch], identification)
        assert len(deals) == 1

    def test_synonym_matching_on_identification(self):
        """Synonyms should work against the identified item name."""
        from agentic_scraper.scanner.interest_matcher import InterestMatcher

        matcher = InterestMatcher()
        listing = Listing(title="PS5 Disc Edition", price=250.0, external_id="1")
        watch = WatchItem(id="w1", interest="playstation 5", max_price=500.0)

        # LLM identified as "PS5" but user's interest is "playstation 5"
        identification = ItemIdentification(
            item_name="PS5 Disc Edition",
            category="electronics/gaming/console",
            confidence=0.9,
        )

        # "playstation 5" synonym "ps5" should match "PS5 Disc Edition"
        deals = matcher.match_with_identification(listing, [watch], identification)
        assert len(deals) == 1

    def test_category_fallback_match(self):
        """If item_name doesn't match, category path should be checked."""
        from agentic_scraper.scanner.interest_matcher import InterestMatcher

        matcher = InterestMatcher()
        listing = Listing(title="Breville Barista Express", price=100.0, external_id="1")
        watch = WatchItem(id="w1", interest="espresso machine", max_price=200.0)

        # item_name doesn't contain "espresso machine" but category does
        identification = ItemIdentification(
            item_name="Breville Barista Express",
            category="appliances/espresso machine",
            confidence=0.9,
        )

        deals = matcher.match_with_identification(listing, [watch], identification)
        assert len(deals) == 1

    def test_price_still_checked(self):
        """Even with LLM matching, price budget should be respected."""
        from agentic_scraper.scanner.interest_matcher import InterestMatcher

        matcher = InterestMatcher()
        listing = Listing(title="PS5 Pro", price=600.0, external_id="1")
        watch = WatchItem(id="w1", interest="PS5", max_price=500.0)

        identification = ItemIdentification(
            item_name="PS5 Pro Console",
            category="electronics/gaming/console",
            confidence=0.9,
        )

        deals = matcher.match_with_identification(listing, [watch], identification)
        assert len(deals) == 0  # Over budget

    def test_vanity_stool_not_coffee_table(self):
        """Vanity stool identified by LLM should NOT match 'coffee table'."""
        from agentic_scraper.scanner.interest_matcher import InterestMatcher

        matcher = InterestMatcher()
        listing = Listing(
            title="SONGMICS Vanity Stool, Set of 2, Round Storage Ottoman Coffee Table",
            price=35.0,
            external_id="1",
        )
        watch = WatchItem(id="w1", interest="coffee table", max_price=100.0)

        # LLM identified this as a vanity stool, not a coffee table
        identification = ItemIdentification(
            item_name="SONGMICS Vanity Stool (Set of 2)",
            category="furniture/seat/vanity stool",
            confidence=0.95,
        )

        deals = matcher.match_with_identification(listing, [watch], identification)
        assert len(deals) == 0
