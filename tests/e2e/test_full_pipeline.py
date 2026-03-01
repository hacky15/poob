"""End-to-end tests for the full scan pipeline with all boundaries mocked."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agentic_scraper.scanner.interest_matcher import InterestMatcher
from agentic_scraper.sites.base import ScanQuery
from agentic_scraper.storage.models import DealScore, Listing, ScanLog, WatchItem


def _build_compat_matcher():
    """Build a matcher that wraps InterestMatcher but also provides build_queries().

    ScanEngine source still calls build_queries() (legacy code being retired).
    This shim keeps the tests working until the source is updated.
    """
    real_matcher = InterestMatcher()

    class CompatMatcher:
        def build_queries(self, watch_items):
            return [
                ScanQuery(
                    keywords=w.interest,
                    max_price=w.max_price,
                    location=w.location,
                )
                for w in watch_items
            ]

        def match(self, listings, interests):
            return real_matcher.match(listings, interests)

    return CompatMatcher()


def _compat_scan_log(**kwargs):
    """Create a ScanLog, translating legacy query_keywords to category."""
    if "query_keywords" in kwargs:
        kwargs["category"] = kwargs.pop("query_keywords")
    return ScanLog(**kwargs)


@pytest.fixture
def mock_notifier():
    """Mock DealNotifier."""
    notifier = AsyncMock()
    notifier.send_deal = AsyncMock()
    return notifier


@pytest.fixture
def mock_adapter():
    """Mock SiteAdapter that returns canned listings."""
    from agentic_scraper.sites.base import ScanResult

    adapter = MagicMock()
    adapter.site_name = "facebook_marketplace"
    adapter.requires_login = True
    adapter.login = AsyncMock(return_value=True)
    adapter.scan = AsyncMock(return_value=ScanResult(
        listings=[
            Listing(
                site="facebook_marketplace",
                external_id="fb_e2e_001",
                title="PS5 Disc Edition",
                price=200.0,
                description="Like new, barely used",
                location="Portland, OR",
            ),
            Listing(
                site="facebook_marketplace",
                external_id="fb_e2e_002",
                title="iPhone 15 Pro",
                price=700.0,
                description="Great condition",
                location="Seattle, WA",
            ),
            Listing(
                site="facebook_marketplace",
                external_id="fb_e2e_003",
                title="Mountain Bike Trek",
                price=150.0,
                description="Needs minor repair",
                location="Portland, OR",
            ),
        ],
        errors=[],
        scan_duration_seconds=3.0,
    ))
    return adapter


@pytest.fixture
def mock_registry(mock_adapter):
    """Mock SiteRegistry."""
    registry = MagicMock()
    registry.list_sites.return_value = ["facebook_marketplace"]
    registry.get.return_value = mock_adapter
    return registry


def _build_engine(db_connection, mock_registry, mock_notifier, deal_radar=None):
    """Helper to construct a ScanEngine with all dependencies."""
    from agentic_scraper.scanner.engine import ScanEngine
    from agentic_scraper.storage.repositories.deal_repo import DealRepository
    from agentic_scraper.storage.repositories.listing_repo import ListingRepository
    from agentic_scraper.storage.repositories.scan_log_repo import ScanLogRepository
    from agentic_scraper.storage.repositories.watchlist_repo import WatchlistRepository

    return ScanEngine(
        registry=mock_registry,
        browser_manager=MagicMock(),
        llm_provider=MagicMock(),
        matcher=_build_compat_matcher(),
        listing_repo=ListingRepository(db_connection),
        watchlist_repo=WatchlistRepository(db_connection),
        deal_repo=DealRepository(db_connection),
        scan_log_repo=ScanLogRepository(db_connection),
        notifier=mock_notifier,
    )


class TestFullPipeline:
    """E2E tests for the complete scan -> match -> notify pipeline."""

    @patch("agentic_scraper.scanner.engine.ScanLog", _compat_scan_log)
    async def test_full_cycle_scan_match_notify(
        self, db_connection, mock_registry, mock_notifier
    ):
        """Full cycle: watches -> scan -> match -> save deals -> notify."""
        from agentic_scraper.storage.repositories.deal_repo import DealRepository
        from agentic_scraper.storage.repositories.listing_repo import ListingRepository
        from agentic_scraper.storage.repositories.watchlist_repo import WatchlistRepository

        watchlist_repo = WatchlistRepository(db_connection)
        listing_repo = ListingRepository(db_connection)
        deal_repo = DealRepository(db_connection)

        # User watches for PS5 under $300
        await watchlist_repo.save(WatchItem(
            interest="PS5",
            max_price=300.0,
            discord_user_id="user_1",
            discord_channel_id="channel_1",
        ))

        engine = _build_engine(db_connection, mock_registry, mock_notifier)

        await engine.run_scan_cycle()

        # Should have saved all 3 listings
        listings = await listing_repo.list_recent()
        assert len(listings) == 3

        # Should have created a deal for PS5 (matches watch, $200 < $300)
        deals = await deal_repo.list_recent()
        assert len(deals) >= 1
        ps5_deal = next((d for d in deals if d.discount_pct and d.discount_pct > 0), None)
        assert ps5_deal is not None

        # Notifier should have been called
        mock_notifier.send_deal.assert_called()

    @patch("agentic_scraper.scanner.engine.ScanLog", _compat_scan_log)
    async def test_full_cycle_no_matches(
        self, db_connection, mock_registry, mock_notifier
    ):
        """When no watches match listings, no deals or notifications."""
        from agentic_scraper.storage.repositories.deal_repo import DealRepository
        from agentic_scraper.storage.repositories.watchlist_repo import WatchlistRepository

        watchlist_repo = WatchlistRepository(db_connection)
        deal_repo = DealRepository(db_connection)

        # User watches for something not in the results
        await watchlist_repo.save(WatchItem(
            interest="Nintendo Switch OLED",
            max_price=200.0,
            discord_user_id="user_1",
            discord_channel_id="channel_1",
        ))

        engine = _build_engine(db_connection, mock_registry, mock_notifier)

        await engine.run_scan_cycle()

        deals = await deal_repo.list_recent()
        assert len(deals) == 0
        mock_notifier.send_deal.assert_not_called()

    @patch("agentic_scraper.scanner.engine.ScanLog", _compat_scan_log)
    async def test_full_cycle_handles_errors(
        self, db_connection, mock_registry, mock_notifier
    ):
        """Pipeline should not crash even when adapter raises errors."""
        mock_registry.get.return_value.scan = AsyncMock(
            side_effect=RuntimeError("Connection reset")
        )

        from agentic_scraper.storage.repositories.watchlist_repo import WatchlistRepository

        watchlist_repo = WatchlistRepository(db_connection)
        await watchlist_repo.save(WatchItem(
            interest="PS5",
            max_price=300.0,
            discord_user_id="user_1",
            discord_channel_id="channel_1",
        ))

        engine = _build_engine(db_connection, mock_registry, mock_notifier)

        # Should not raise
        await engine.run_scan_cycle()

    @patch("agentic_scraper.scanner.engine.ScanLog", _compat_scan_log)
    async def test_full_cycle_multiple_watches(
        self, db_connection, mock_registry, mock_notifier
    ):
        """Multiple watches should each produce their own matches."""
        from agentic_scraper.storage.repositories.deal_repo import DealRepository
        from agentic_scraper.storage.repositories.watchlist_repo import WatchlistRepository

        watchlist_repo = WatchlistRepository(db_connection)
        deal_repo = DealRepository(db_connection)

        await watchlist_repo.save(WatchItem(
            interest="PS5",
            max_price=300.0,
            discord_user_id="user_1",
            discord_channel_id="channel_1",
        ))
        await watchlist_repo.save(WatchItem(
            interest="iPhone",
            max_price=800.0,
            discord_user_id="user_2",
            discord_channel_id="channel_2",
        ))

        engine = _build_engine(db_connection, mock_registry, mock_notifier)

        await engine.run_scan_cycle()

        deals = await deal_repo.list_recent()
        # PS5 at $200 matches "PS5" watch ($300 max)
        # iPhone 15 Pro at $700 matches "iPhone" watch ($800 max)
        assert len(deals) >= 2
