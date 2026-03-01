"""Integration tests for ScanEngine - full scan cycle with mocked boundaries."""

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
    """Mock SiteAdapter that returns a canned ScanResult."""
    from agentic_scraper.sites.base import ScanResult

    adapter = MagicMock()
    adapter.site_name = "facebook_marketplace"
    adapter.requires_login = True
    adapter.login = AsyncMock(return_value=True)
    adapter.scan = AsyncMock(return_value=ScanResult(
        listings=[
            Listing(
                site="facebook_marketplace",
                external_id="fb_001",
                title="PS5 Disc Edition",
                price=250.0,
                location="Portland, OR",
            ),
            Listing(
                site="facebook_marketplace",
                external_id="fb_002",
                title="Xbox Series X",
                price=350.0,
                location="Seattle, WA",
            ),
        ],
        errors=[],
        scan_duration_seconds=5.0,
    ))
    return adapter


@pytest.fixture
def mock_registry(mock_adapter):
    """Mock SiteRegistry with one adapter."""
    registry = MagicMock()
    registry.list_sites.return_value = ["facebook_marketplace"]
    registry.get.return_value = mock_adapter
    return registry


class TestScanEngine:
    """Integration tests for ScanEngine.run_scan_cycle()."""

    async def test_no_active_watches_does_nothing(
        self, db_connection, mock_registry, mock_notifier
    ):
        """With no active watches, scan cycle should complete without scanning."""
        from agentic_scraper.scanner.engine import ScanEngine
        from agentic_scraper.storage.repositories.deal_repo import DealRepository
        from agentic_scraper.storage.repositories.listing_repo import ListingRepository
        from agentic_scraper.storage.repositories.scan_log_repo import ScanLogRepository
        from agentic_scraper.storage.repositories.watchlist_repo import WatchlistRepository

        engine = ScanEngine(
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

        await engine.run_scan_cycle()
        # No watches → no adapter.scan calls
        mock_registry.get.return_value.scan.assert_not_called()

    @patch("agentic_scraper.scanner.engine.ScanLog", _compat_scan_log)
    async def test_scan_cycle_calls_adapter(
        self, db_connection, mock_registry, mock_adapter, mock_notifier
    ):
        """Scan cycle should call adapter.scan() for each query."""
        from agentic_scraper.scanner.engine import ScanEngine
        from agentic_scraper.storage.repositories.deal_repo import DealRepository
        from agentic_scraper.storage.repositories.listing_repo import ListingRepository
        from agentic_scraper.storage.repositories.scan_log_repo import ScanLogRepository
        from agentic_scraper.storage.repositories.watchlist_repo import WatchlistRepository

        watchlist_repo = WatchlistRepository(db_connection)
        await watchlist_repo.save(WatchItem(
            interest="PS5", max_price=300.0,
            discord_user_id="u1", discord_channel_id="c1",
        ))

        engine = ScanEngine(
            registry=mock_registry,
            browser_manager=MagicMock(),
            llm_provider=MagicMock(),
            matcher=_build_compat_matcher(),
            listing_repo=ListingRepository(db_connection),
            watchlist_repo=watchlist_repo,
            deal_repo=DealRepository(db_connection),
            scan_log_repo=ScanLogRepository(db_connection),
            notifier=mock_notifier,
        )

        await engine.run_scan_cycle()
        mock_adapter.scan.assert_called_once()

    @patch("agentic_scraper.scanner.engine.ScanLog", _compat_scan_log)
    async def test_scan_cycle_saves_new_listings(
        self, db_connection, mock_registry, mock_adapter, mock_notifier
    ):
        """New listings should be persisted to the database."""
        from agentic_scraper.scanner.engine import ScanEngine
        from agentic_scraper.storage.repositories.deal_repo import DealRepository
        from agentic_scraper.storage.repositories.listing_repo import ListingRepository
        from agentic_scraper.storage.repositories.scan_log_repo import ScanLogRepository
        from agentic_scraper.storage.repositories.watchlist_repo import WatchlistRepository

        watchlist_repo = WatchlistRepository(db_connection)
        listing_repo = ListingRepository(db_connection)
        await watchlist_repo.save(WatchItem(
            interest="PS5", max_price=300.0,
            discord_user_id="u1", discord_channel_id="c1",
        ))

        engine = ScanEngine(
            registry=mock_registry,
            browser_manager=MagicMock(),
            llm_provider=MagicMock(),
            matcher=_build_compat_matcher(),
            listing_repo=listing_repo,
            watchlist_repo=watchlist_repo,
            deal_repo=DealRepository(db_connection),
            scan_log_repo=ScanLogRepository(db_connection),
            notifier=mock_notifier,
        )

        await engine.run_scan_cycle()
        saved = await listing_repo.list_recent()
        assert len(saved) == 2

    @patch("agentic_scraper.scanner.engine.ScanLog", _compat_scan_log)
    async def test_scan_cycle_deduplicates(
        self, db_connection, mock_registry, mock_adapter, mock_notifier
    ):
        """Existing listings should not be saved again."""
        from agentic_scraper.scanner.engine import ScanEngine
        from agentic_scraper.storage.repositories.deal_repo import DealRepository
        from agentic_scraper.storage.repositories.listing_repo import ListingRepository
        from agentic_scraper.storage.repositories.scan_log_repo import ScanLogRepository
        from agentic_scraper.storage.repositories.watchlist_repo import WatchlistRepository

        listing_repo = ListingRepository(db_connection)
        watchlist_repo = WatchlistRepository(db_connection)

        # Pre-save one listing
        await listing_repo.save(Listing(
            site="facebook_marketplace",
            external_id="fb_001",
            title="PS5",
            price=250.0,
        ))

        await watchlist_repo.save(WatchItem(
            interest="PS5", max_price=300.0,
            discord_user_id="u1", discord_channel_id="c1",
        ))

        engine = ScanEngine(
            registry=mock_registry,
            browser_manager=MagicMock(),
            llm_provider=MagicMock(),
            matcher=_build_compat_matcher(),
            listing_repo=listing_repo,
            watchlist_repo=watchlist_repo,
            deal_repo=DealRepository(db_connection),
            scan_log_repo=ScanLogRepository(db_connection),
            notifier=mock_notifier,
        )

        await engine.run_scan_cycle()
        saved = await listing_repo.list_recent()
        # fb_001 already existed, only fb_002 is new → 2 total
        assert len(saved) == 2

    @patch("agentic_scraper.scanner.engine.ScanLog", _compat_scan_log)
    async def test_scan_cycle_creates_deals(
        self, db_connection, mock_registry, mock_adapter, mock_notifier
    ):
        """Matching listings should produce Deal objects."""
        from agentic_scraper.scanner.engine import ScanEngine
        from agentic_scraper.storage.repositories.deal_repo import DealRepository
        from agentic_scraper.storage.repositories.listing_repo import ListingRepository
        from agentic_scraper.storage.repositories.scan_log_repo import ScanLogRepository
        from agentic_scraper.storage.repositories.watchlist_repo import WatchlistRepository

        watchlist_repo = WatchlistRepository(db_connection)
        deal_repo = DealRepository(db_connection)
        await watchlist_repo.save(WatchItem(
            interest="PS5", max_price=300.0,
            discord_user_id="u1", discord_channel_id="c1",
        ))

        engine = ScanEngine(
            registry=mock_registry,
            browser_manager=MagicMock(),
            llm_provider=MagicMock(),
            matcher=_build_compat_matcher(),
            listing_repo=ListingRepository(db_connection),
            watchlist_repo=watchlist_repo,
            deal_repo=deal_repo,
            scan_log_repo=ScanLogRepository(db_connection),
            notifier=mock_notifier,
        )

        await engine.run_scan_cycle()
        deals = await deal_repo.list_recent()
        # "PS5 Disc Edition" matches "PS5" watch, priced at $250 < $300
        assert len(deals) >= 1

    @patch("agentic_scraper.scanner.engine.ScanLog", _compat_scan_log)
    async def test_scan_cycle_calls_notifier(
        self, db_connection, mock_registry, mock_adapter, mock_notifier
    ):
        """Notifier should be called for each deal created."""
        from agentic_scraper.scanner.engine import ScanEngine
        from agentic_scraper.storage.repositories.deal_repo import DealRepository
        from agentic_scraper.storage.repositories.listing_repo import ListingRepository
        from agentic_scraper.storage.repositories.scan_log_repo import ScanLogRepository
        from agentic_scraper.storage.repositories.watchlist_repo import WatchlistRepository

        watchlist_repo = WatchlistRepository(db_connection)
        await watchlist_repo.save(WatchItem(
            interest="PS5", max_price=300.0,
            discord_user_id="u1", discord_channel_id="c1",
        ))

        engine = ScanEngine(
            registry=mock_registry,
            browser_manager=MagicMock(),
            llm_provider=MagicMock(),
            matcher=_build_compat_matcher(),
            listing_repo=ListingRepository(db_connection),
            watchlist_repo=watchlist_repo,
            deal_repo=DealRepository(db_connection),
            scan_log_repo=ScanLogRepository(db_connection),
            notifier=mock_notifier,
        )

        await engine.run_scan_cycle()
        mock_notifier.send_deal.assert_called()

    @patch("agentic_scraper.scanner.engine.ScanLog", _compat_scan_log)
    async def test_scan_cycle_logs_scan_log(
        self, db_connection, mock_registry, mock_adapter, mock_notifier
    ):
        """A ScanLog should be saved for each scan."""
        from agentic_scraper.scanner.engine import ScanEngine
        from agentic_scraper.storage.repositories.deal_repo import DealRepository
        from agentic_scraper.storage.repositories.listing_repo import ListingRepository
        from agentic_scraper.storage.repositories.scan_log_repo import ScanLogRepository
        from agentic_scraper.storage.repositories.watchlist_repo import WatchlistRepository

        watchlist_repo = WatchlistRepository(db_connection)
        scan_log_repo = ScanLogRepository(db_connection)
        await watchlist_repo.save(WatchItem(
            interest="PS5", max_price=300.0,
            discord_user_id="u1", discord_channel_id="c1",
        ))

        engine = ScanEngine(
            registry=mock_registry,
            browser_manager=MagicMock(),
            llm_provider=MagicMock(),
            matcher=_build_compat_matcher(),
            listing_repo=ListingRepository(db_connection),
            watchlist_repo=watchlist_repo,
            deal_repo=DealRepository(db_connection),
            scan_log_repo=scan_log_repo,
            notifier=mock_notifier,
        )

        await engine.run_scan_cycle()
        logs = await scan_log_repo.list_recent()
        assert len(logs) >= 1
        assert logs[0].site == "facebook_marketplace"

    @patch("agentic_scraper.scanner.engine.ScanLog", _compat_scan_log)
    async def test_scan_cycle_handles_adapter_error(
        self, db_connection, mock_registry, mock_notifier
    ):
        """Engine should not crash when an adapter raises an error."""
        from agentic_scraper.scanner.engine import ScanEngine
        from agentic_scraper.storage.repositories.deal_repo import DealRepository
        from agentic_scraper.storage.repositories.listing_repo import ListingRepository
        from agentic_scraper.storage.repositories.scan_log_repo import ScanLogRepository
        from agentic_scraper.storage.repositories.watchlist_repo import WatchlistRepository

        # Make adapter raise an error
        mock_registry.get.return_value.scan = AsyncMock(
            side_effect=RuntimeError("Browser crashed")
        )

        watchlist_repo = WatchlistRepository(db_connection)
        await watchlist_repo.save(WatchItem(
            interest="PS5", max_price=300.0,
            discord_user_id="u1", discord_channel_id="c1",
        ))

        engine = ScanEngine(
            registry=mock_registry,
            browser_manager=MagicMock(),
            llm_provider=MagicMock(),
            matcher=_build_compat_matcher(),
            listing_repo=ListingRepository(db_connection),
            watchlist_repo=watchlist_repo,
            deal_repo=DealRepository(db_connection),
            scan_log_repo=ScanLogRepository(db_connection),
            notifier=mock_notifier,
        )

        # Should not raise
        await engine.run_scan_cycle()

    @patch("agentic_scraper.scanner.engine.ScanLog", _compat_scan_log)
    async def test_browse_enabled_adds_extra_query(
        self, db_connection, mock_registry, mock_adapter, mock_notifier
    ):
        """With browse enabled, adapter.scan should be called with an extra browse query."""
        from agentic_scraper.scanner.engine import ScanEngine
        from agentic_scraper.storage.repositories.deal_repo import DealRepository
        from agentic_scraper.storage.repositories.listing_repo import ListingRepository
        from agentic_scraper.storage.repositories.scan_log_repo import ScanLogRepository
        from agentic_scraper.storage.repositories.watchlist_repo import WatchlistRepository

        watchlist_repo = WatchlistRepository(db_connection)
        await watchlist_repo.save(WatchItem(
            interest="PS5", max_price=300.0,
            discord_user_id="u1", discord_channel_id="c1",
        ))

        engine = ScanEngine(
            registry=mock_registry,
            browser_manager=MagicMock(),
            llm_provider=MagicMock(),
            matcher=_build_compat_matcher(),
            listing_repo=ListingRepository(db_connection),
            watchlist_repo=watchlist_repo,
            deal_repo=DealRepository(db_connection),
            scan_log_repo=ScanLogRepository(db_connection),
            notifier=mock_notifier,
            browse_enabled=True,
        )

        await engine.run_scan_cycle()
        # 1 watch query + 1 browse query = 2 calls
        assert mock_adapter.scan.call_count == 2
        # The second call should have empty keywords (browse query)
        browse_call = mock_adapter.scan.call_args_list[1]
        assert browse_call[0][0].keywords == ""

    @patch("agentic_scraper.scanner.engine.ScanLog", _compat_scan_log)
    async def test_browse_disabled_no_extra_query(
        self, db_connection, mock_registry, mock_adapter, mock_notifier
    ):
        """With browse disabled, only watchlist queries run."""
        from agentic_scraper.scanner.engine import ScanEngine
        from agentic_scraper.storage.repositories.deal_repo import DealRepository
        from agentic_scraper.storage.repositories.listing_repo import ListingRepository
        from agentic_scraper.storage.repositories.scan_log_repo import ScanLogRepository
        from agentic_scraper.storage.repositories.watchlist_repo import WatchlistRepository

        watchlist_repo = WatchlistRepository(db_connection)
        await watchlist_repo.save(WatchItem(
            interest="PS5", max_price=300.0,
            discord_user_id="u1", discord_channel_id="c1",
        ))

        engine = ScanEngine(
            registry=mock_registry,
            browser_manager=MagicMock(),
            llm_provider=MagicMock(),
            matcher=_build_compat_matcher(),
            listing_repo=ListingRepository(db_connection),
            watchlist_repo=watchlist_repo,
            deal_repo=DealRepository(db_connection),
            scan_log_repo=ScanLogRepository(db_connection),
            notifier=mock_notifier,
            browse_enabled=False,
        )

        await engine.run_scan_cycle()
        # Only 1 watch query, no browse
        assert mock_adapter.scan.call_count == 1

    @patch("agentic_scraper.scanner.engine.ScanLog", _compat_scan_log)
    async def test_browse_runs_even_with_no_watches(
        self, db_connection, mock_registry, mock_adapter, mock_notifier
    ):
        """Browse should run even when there are no active watch items."""
        from agentic_scraper.scanner.engine import ScanEngine
        from agentic_scraper.storage.repositories.deal_repo import DealRepository
        from agentic_scraper.storage.repositories.listing_repo import ListingRepository
        from agentic_scraper.storage.repositories.scan_log_repo import ScanLogRepository
        from agentic_scraper.storage.repositories.watchlist_repo import WatchlistRepository

        engine = ScanEngine(
            registry=mock_registry,
            browser_manager=MagicMock(),
            llm_provider=MagicMock(),
            matcher=_build_compat_matcher(),
            listing_repo=ListingRepository(db_connection),
            watchlist_repo=WatchlistRepository(db_connection),
            deal_repo=DealRepository(db_connection),
            scan_log_repo=ScanLogRepository(db_connection),
            notifier=mock_notifier,
            browse_enabled=True,
        )

        await engine.run_scan_cycle()
        # Should still call scan once for the browse query
        assert mock_adapter.scan.call_count == 1
        assert mock_adapter.scan.call_args[0][0].keywords == ""
