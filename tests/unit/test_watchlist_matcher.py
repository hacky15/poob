"""Tests for WatchlistMatcher - keyword matching and price filtering."""

from __future__ import annotations

import pytest

from agentic_scraper.storage.models import DealScore, Listing, WatchItem


class TestBuildQueries:
    """Tests for WatchlistMatcher.build_queries()."""

    def test_builds_query_from_watch_item(self):
        """Should convert a WatchItem to a ScanQuery."""
        from agentic_scraper.scanner.watchlist import WatchlistMatcher

        matcher = WatchlistMatcher()
        watch = WatchItem(
            keywords="PS5",
            max_price=300.0,
            location="Portland, OR",
            radius_miles=25,
        )
        queries = matcher.build_queries([watch])
        assert len(queries) == 1
        assert queries[0].keywords == "PS5"
        assert queries[0].max_price == 300.0
        assert queries[0].location == "Portland, OR"

    def test_builds_multiple_queries(self):
        """Should produce one query per active watch item."""
        from agentic_scraper.scanner.watchlist import WatchlistMatcher

        matcher = WatchlistMatcher()
        watches = [
            WatchItem(keywords="PS5", max_price=300.0),
            WatchItem(keywords="Xbox", max_price=400.0),
        ]
        queries = matcher.build_queries(watches)
        assert len(queries) == 2

    def test_builds_query_without_optional_fields(self):
        """Should handle watch items with no price/location set."""
        from agentic_scraper.scanner.watchlist import WatchlistMatcher

        matcher = WatchlistMatcher()
        watch = WatchItem(keywords="laptop")
        queries = matcher.build_queries([watch])
        assert len(queries) == 1
        assert queries[0].max_price is None
        assert queries[0].location is None


class TestMatch:
    """Tests for WatchlistMatcher.match()."""

    def test_keyword_matches_title(self):
        """Listing title containing watch keywords should match."""
        from agentic_scraper.scanner.watchlist import WatchlistMatcher

        matcher = WatchlistMatcher()
        listing = Listing(title="PlayStation 5 Disc Edition", price=250.0, external_id="1")
        watch = WatchItem(id="w1", keywords="PlayStation 5", max_price=300.0)

        deals = matcher.match([listing], [watch])
        assert len(deals) == 1
        assert deals[0].listing_id == listing.id or deals[0].watch_item_id == "w1"

    def test_keyword_case_insensitive(self):
        """Matching should be case-insensitive."""
        from agentic_scraper.scanner.watchlist import WatchlistMatcher

        matcher = WatchlistMatcher()
        listing = Listing(title="PLAYSTATION 5", price=250.0, external_id="1")
        watch = WatchItem(id="w1", keywords="playstation 5", max_price=300.0)

        deals = matcher.match([listing], [watch])
        assert len(deals) == 1

    def test_no_match_when_keywords_differ(self):
        """Non-matching keywords should produce no deals."""
        from agentic_scraper.scanner.watchlist import WatchlistMatcher

        matcher = WatchlistMatcher()
        listing = Listing(title="Xbox Series X", price=300.0, external_id="1")
        watch = WatchItem(id="w1", keywords="PlayStation 5", max_price=400.0)

        deals = matcher.match([listing], [watch])
        assert len(deals) == 0

    def test_price_within_budget(self):
        """Listing priced at or below max_price should match."""
        from agentic_scraper.scanner.watchlist import WatchlistMatcher

        matcher = WatchlistMatcher()
        listing = Listing(title="PS5", price=250.0, external_id="1")
        watch = WatchItem(id="w1", keywords="PS5", max_price=300.0)

        deals = matcher.match([listing], [watch])
        assert len(deals) == 1

    def test_price_over_budget_excluded(self):
        """Listing priced above max_price should not match."""
        from agentic_scraper.scanner.watchlist import WatchlistMatcher

        matcher = WatchlistMatcher()
        listing = Listing(title="PS5", price=500.0, external_id="1")
        watch = WatchItem(id="w1", keywords="PS5", max_price=300.0)

        deals = matcher.match([listing], [watch])
        assert len(deals) == 0

    def test_no_max_price_always_matches(self):
        """Watch without max_price should match any price."""
        from agentic_scraper.scanner.watchlist import WatchlistMatcher

        matcher = WatchlistMatcher()
        listing = Listing(title="PS5", price=999.0, external_id="1")
        watch = WatchItem(id="w1", keywords="PS5", max_price=None)

        deals = matcher.match([listing], [watch])
        assert len(deals) == 1

    def test_calculates_discount_percentage(self):
        """Deal should have discount_pct when max_price and price are set."""
        from agentic_scraper.scanner.watchlist import WatchlistMatcher

        matcher = WatchlistMatcher()
        listing = Listing(title="PS5", price=200.0, external_id="1")
        watch = WatchItem(id="w1", keywords="PS5", max_price=400.0)

        deals = matcher.match([listing], [watch])
        assert len(deals) == 1
        # 200 is 50% of 400 → 50% discount
        assert deals[0].discount_pct == pytest.approx(50.0)

    def test_assigns_deal_score_great(self):
        """40-60% discount should get GREAT score."""
        from agentic_scraper.scanner.watchlist import WatchlistMatcher

        matcher = WatchlistMatcher()
        listing = Listing(title="PS5", price=200.0, external_id="1")
        watch = WatchItem(id="w1", keywords="PS5", max_price=400.0)

        deals = matcher.match([listing], [watch])
        assert deals[0].score == DealScore.GREAT

    def test_assigns_deal_score_good(self):
        """20-40% discount should get GOOD score."""
        from agentic_scraper.scanner.watchlist import WatchlistMatcher

        matcher = WatchlistMatcher()
        listing = Listing(title="PS5", price=280.0, external_id="1")
        watch = WatchItem(id="w1", keywords="PS5", max_price=400.0)

        deals = matcher.match([listing], [watch])
        # 280/400 = 30% discount → GOOD
        assert deals[0].score == DealScore.GOOD
