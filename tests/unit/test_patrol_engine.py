"""Tests for PatrolEngine - the 4-phase GraphQL-first patrol cycle."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agentic_scraper.config import AppConfig
from agentic_scraper.scanner.interest_matcher import InterestMatcher
from agentic_scraper.storage.models import Deal, DealScore, Listing, ScanLog, WatchItem


# --- Fixtures ---


def _make_listing(
    external_id: str = "111",
    title: str = "PS5 Console",
    price: float = 300.0,
    **kwargs,
) -> Listing:
    return Listing(
        id=f"listing-{external_id}",
        site="facebook_marketplace",
        external_id=external_id,
        title=title,
        price=price,
        listing_url=f"https://facebook.com/marketplace/item/{external_id}",
        **kwargs,
    )


def _make_interest(
    id: str = "watch-1",
    interest: str = "PS5",
    max_price: float = 500.0,
) -> WatchItem:
    return WatchItem(id=id, interest=interest, max_price=max_price)


@pytest.fixture
def mock_config():
    return MagicMock(
        spec=AppConfig,
        patrol_categories=["electronics", "furniture"],
        patrol_inter_category_delay_min_ms=100,
        patrol_inter_category_delay_max_ms=200,
        patrol_inter_listing_delay_min_ms=50,
        patrol_inter_listing_delay_max_ms=100,
        patrol_base_radius_miles=20,
        patrol_radius_jitter=4,
        patrol_days_since_listed=1,
        patrol_include_all_categories=True,
        patrol_deep_inspect_enabled=True,
        patrol_radius_oscillation_enabled=False,
        patrol_sweep_mode="unified",
        deal_radar_max_evaluations=10,
        deal_radar_min_score="good",
    )


@pytest.fixture
def mock_browser_manager():
    mgr = AsyncMock()
    page = AsyncMock()
    mgr.get_page = AsyncMock(return_value=page)
    # get_session() for GraphQL interceptor
    session = MagicMock()
    mgr.get_session = MagicMock(return_value=session)
    return mgr


@pytest.fixture
def mock_listing_repo():
    repo = AsyncMock()
    repo.exists = AsyncMock(return_value=False)
    repo.save = AsyncMock(side_effect=lambda listing: listing)
    repo.filter_new_ids = AsyncMock(return_value=set())
    return repo


@pytest.fixture
def mock_watchlist_repo():
    repo = AsyncMock()
    repo.list_active = AsyncMock(return_value=[])
    return repo


@pytest.fixture
def mock_deal_repo():
    return AsyncMock()


@pytest.fixture
def mock_scan_log_repo():
    return AsyncMock()


@pytest.fixture
def mock_notifier():
    return AsyncMock()


@pytest.fixture
def mock_smart_deal_radar():
    radar = AsyncMock()
    radar.evaluate = AsyncMock(return_value=None)
    return radar


@pytest.fixture
def interest_matcher():
    return InterestMatcher()


@pytest.fixture
def patrol_engine(
    mock_config,
    mock_browser_manager,
    mock_listing_repo,
    mock_watchlist_repo,
    mock_deal_repo,
    mock_scan_log_repo,
    mock_notifier,
    mock_smart_deal_radar,
    interest_matcher,
):
    from agentic_scraper.scanner.patrol_engine import PatrolEngine

    return PatrolEngine(
        browser_manager=mock_browser_manager,
        listing_repo=mock_listing_repo,
        watchlist_repo=mock_watchlist_repo,
        deal_repo=mock_deal_repo,
        scan_log_repo=mock_scan_log_repo,
        notifier=mock_notifier,
        interest_matcher=interest_matcher,
        smart_deal_radar=mock_smart_deal_radar,
        config=mock_config,
    )


# --- Phase 1: Sweep + Intercept ---


class TestSweepAndIntercept:
    @pytest.mark.asyncio
    async def test_unified_mode_single_sweep(self, patrol_engine, mock_listing_repo):
        """Unified mode should sweep only once (None category)."""
        mock_listing_repo.filter_new_ids = AsyncMock(return_value=set())

        with patch.object(
            patrol_engine._scanner, "sweep_category", new_callable=AsyncMock
        ) as mock_sweep, patch.object(
            patrol_engine._interceptor, "start", new_callable=AsyncMock,
        ), patch.object(
            patrol_engine._interceptor, "drain", return_value=[],
        ), patch.object(
            patrol_engine._interceptor, "stop", new_callable=AsyncMock,
        ):
            mock_sweep.return_value = []
            result = await patrol_engine.run_patrol_cycle()

        # Unified mode: only 1 sweep call (None = all listings)
        assert mock_sweep.call_count == 1

    @pytest.mark.asyncio
    async def test_categories_mode_sweeps_all(self, patrol_engine, mock_config, mock_listing_repo):
        """Categories mode should sweep each configured category + all."""
        mock_config.patrol_sweep_mode = "categories"
        patrol_engine._sweep_mode = "categories"
        mock_listing_repo.filter_new_ids = AsyncMock(return_value=set())

        with patch.object(
            patrol_engine._scanner, "sweep_category", new_callable=AsyncMock
        ) as mock_sweep, patch.object(
            patrol_engine._interceptor, "start", new_callable=AsyncMock,
        ), patch.object(
            patrol_engine._interceptor, "drain", return_value=[],
        ), patch.object(
            patrol_engine._interceptor, "stop", new_callable=AsyncMock,
        ):
            mock_sweep.return_value = []
            result = await patrol_engine.run_patrol_cycle()

        # electronics + furniture + all(None) = 3 calls
        assert mock_sweep.call_count == 3

    @pytest.mark.asyncio
    async def test_graphql_data_preferred_over_dom(self, patrol_engine, mock_listing_repo):
        """When GraphQL intercept returns data, it should be used as primary source."""
        from agentic_scraper.browser.graphql_interceptor import GraphQLListingData

        gql_data = [
            GraphQLListingData(
                external_id="gql-111",
                title="PS5 from GraphQL",
                price=250.0,
                listing_url="https://facebook.com/marketplace/item/gql-111",
            )
        ]

        mock_listing_repo.filter_new_ids = AsyncMock(return_value={"gql-111"})

        with patch.object(
            patrol_engine._scanner, "sweep_category", new_callable=AsyncMock
        ) as mock_sweep, patch.object(
            patrol_engine._interceptor, "start", new_callable=AsyncMock,
        ), patch.object(
            patrol_engine._interceptor, "drain", return_value=gql_data,
        ), patch.object(
            patrol_engine._interceptor, "stop", new_callable=AsyncMock,
        ):
            mock_sweep.return_value = []
            result = await patrol_engine.run_patrol_cycle()

        assert result.data_source == "graphql"
        assert result.new_listings == 1

    @pytest.mark.asyncio
    async def test_dom_fallback_when_graphql_empty(self, patrol_engine, mock_listing_repo):
        """When GraphQL returns nothing, DOM data should be used."""
        dom_listing = _make_listing("dom-111", "Table from DOM", 50.0)
        mock_listing_repo.filter_new_ids = AsyncMock(return_value={"dom-111"})

        with patch.object(
            patrol_engine._scanner, "sweep_category", new_callable=AsyncMock
        ) as mock_sweep, patch.object(
            patrol_engine._interceptor, "start", new_callable=AsyncMock,
        ), patch.object(
            patrol_engine._interceptor, "drain", return_value=[],
        ), patch.object(
            patrol_engine._interceptor, "stop", new_callable=AsyncMock,
        ):
            mock_sweep.return_value = [dom_listing]
            result = await patrol_engine.run_patrol_cycle()

        assert result.data_source == "dom_fallback"
        assert result.new_listings == 1

    @pytest.mark.asyncio
    async def test_interceptor_failure_uses_dom(self, patrol_engine, mock_listing_repo):
        """If interceptor start fails, should still work with DOM data."""
        dom_listing = _make_listing("111")
        mock_listing_repo.filter_new_ids = AsyncMock(return_value={"111"})

        with patch.object(
            patrol_engine._scanner, "sweep_category", new_callable=AsyncMock
        ) as mock_sweep, patch.object(
            patrol_engine._interceptor, "start", new_callable=AsyncMock,
            side_effect=Exception("CDP not available"),
        ):
            mock_sweep.return_value = [dom_listing]
            result = await patrol_engine.run_patrol_cycle()

        assert result.data_source == "dom_fallback"
        assert result.new_listings == 1


# --- Phase 2: Batch Dedup ---


class TestBatchDedup:
    @pytest.mark.asyncio
    async def test_batch_dedup_saves_new_listings(
        self, patrol_engine, mock_listing_repo,
    ):
        listings = [_make_listing("111"), _make_listing("222")]
        # Both are new
        mock_listing_repo.filter_new_ids = AsyncMock(return_value={"111", "222"})

        with patch.object(
            patrol_engine._scanner, "sweep_category", new_callable=AsyncMock
        ) as mock_sweep, patch.object(
            patrol_engine._interceptor, "start", new_callable=AsyncMock,
        ), patch.object(
            patrol_engine._interceptor, "drain", return_value=[],
        ), patch.object(
            patrol_engine._interceptor, "stop", new_callable=AsyncMock,
        ):
            mock_sweep.return_value = listings
            result = await patrol_engine.run_patrol_cycle()

        assert mock_listing_repo.save.call_count == 2
        assert result.new_listings == 2

    @pytest.mark.asyncio
    async def test_batch_dedup_skips_existing(
        self, patrol_engine, mock_listing_repo,
    ):
        listings = [_make_listing("111"), _make_listing("222")]
        # Only 222 is new
        mock_listing_repo.filter_new_ids = AsyncMock(return_value={"222"})

        with patch.object(
            patrol_engine._scanner, "sweep_category", new_callable=AsyncMock
        ) as mock_sweep, patch.object(
            patrol_engine._interceptor, "start", new_callable=AsyncMock,
        ), patch.object(
            patrol_engine._interceptor, "drain", return_value=[],
        ), patch.object(
            patrol_engine._interceptor, "stop", new_callable=AsyncMock,
        ):
            mock_sweep.return_value = listings
            result = await patrol_engine.run_patrol_cycle()

        assert mock_listing_repo.save.call_count == 1
        assert result.new_listings == 1

    @pytest.mark.asyncio
    async def test_batch_dedup_fallback_to_individual(
        self, patrol_engine, mock_listing_repo,
    ):
        """If batch dedup fails, should fall back to individual exists() calls."""
        listings = [_make_listing("111")]
        mock_listing_repo.filter_new_ids = AsyncMock(side_effect=Exception("DB error"))
        mock_listing_repo.exists = AsyncMock(return_value=False)

        with patch.object(
            patrol_engine._scanner, "sweep_category", new_callable=AsyncMock
        ) as mock_sweep, patch.object(
            patrol_engine._interceptor, "start", new_callable=AsyncMock,
        ), patch.object(
            patrol_engine._interceptor, "drain", return_value=[],
        ), patch.object(
            patrol_engine._interceptor, "stop", new_callable=AsyncMock,
        ):
            mock_sweep.return_value = listings
            result = await patrol_engine.run_patrol_cycle()

        # Should still save via individual fallback
        assert mock_listing_repo.save.call_count == 1


# --- Phase 3: Evaluation ---


class TestEvaluation:
    @pytest.mark.asyncio
    async def test_runs_smart_deal_radar(
        self, patrol_engine, mock_listing_repo, mock_smart_deal_radar,
    ):
        listings = [_make_listing("111")]
        mock_listing_repo.filter_new_ids = AsyncMock(return_value={"111"})

        with patch.object(
            patrol_engine._scanner, "sweep_category", new_callable=AsyncMock
        ) as mock_sweep, patch.object(
            patrol_engine._interceptor, "start", new_callable=AsyncMock,
        ), patch.object(
            patrol_engine._interceptor, "drain", return_value=[],
        ), patch.object(
            patrol_engine._interceptor, "stop", new_callable=AsyncMock,
        ):
            mock_sweep.return_value = listings
            await patrol_engine.run_patrol_cycle()

        assert mock_smart_deal_radar.evaluate.call_count >= 1

    @pytest.mark.asyncio
    async def test_interest_match_creates_deal_when_radar_returns_none(
        self,
        patrol_engine,
        mock_listing_repo,
        mock_watchlist_repo,
        mock_smart_deal_radar,
        mock_deal_repo,
    ):
        listing = _make_listing("111", title="PS5 Console Bundle", price=300.0)
        interest = _make_interest("watch-1", "PS5", 500.0)

        mock_listing_repo.filter_new_ids = AsyncMock(return_value={"111"})
        mock_watchlist_repo.list_active = AsyncMock(return_value=[interest])
        mock_smart_deal_radar.evaluate = AsyncMock(return_value=None)

        with patch.object(
            patrol_engine._scanner, "sweep_category", new_callable=AsyncMock
        ) as mock_sweep, patch.object(
            patrol_engine._interceptor, "start", new_callable=AsyncMock,
        ), patch.object(
            patrol_engine._interceptor, "drain", return_value=[],
        ), patch.object(
            patrol_engine._interceptor, "stop", new_callable=AsyncMock,
        ):
            mock_sweep.return_value = [listing]
            await patrol_engine.run_patrol_cycle()

        assert mock_deal_repo.save.call_count >= 1

    @pytest.mark.asyncio
    async def test_radar_deal_takes_precedence_over_interest_match(
        self,
        patrol_engine,
        mock_listing_repo,
        mock_watchlist_repo,
        mock_smart_deal_radar,
        mock_deal_repo,
    ):
        listing = _make_listing("111", title="PS5 Console", price=200.0)
        interest = _make_interest("watch-1", "PS5", 500.0)
        radar_deal = Deal(
            listing_id="listing-111",
            score=DealScore.GREAT,
            estimated_market_price=450.0,
            discount_pct=55.5,
            llm_reasoning="Great deal on PS5",
        )

        mock_listing_repo.filter_new_ids = AsyncMock(return_value={"111"})
        mock_watchlist_repo.list_active = AsyncMock(return_value=[interest])
        mock_smart_deal_radar.evaluate = AsyncMock(return_value=radar_deal)

        with patch.object(
            patrol_engine._scanner, "sweep_category", new_callable=AsyncMock
        ) as mock_sweep, patch.object(
            patrol_engine._interceptor, "start", new_callable=AsyncMock,
        ), patch.object(
            patrol_engine._interceptor, "drain", return_value=[],
        ), patch.object(
            patrol_engine._interceptor, "stop", new_callable=AsyncMock,
        ):
            mock_sweep.return_value = [listing]
            await patrol_engine.run_patrol_cycle()

        saved_deal = mock_deal_repo.save.call_args[0][0]
        assert saved_deal.score == DealScore.GREAT


# --- Phase 4: Notify ---


class TestNotify:
    @pytest.mark.asyncio
    async def test_notifies_on_deals(
        self,
        patrol_engine,
        mock_listing_repo,
        mock_smart_deal_radar,
        mock_notifier,
        mock_deal_repo,
    ):
        listing = _make_listing("111")
        deal = Deal(
            listing_id="listing-111",
            score=DealScore.GOOD,
            estimated_market_price=500.0,
            discount_pct=40.0,
            llm_reasoning="Good deal",
        )
        mock_listing_repo.filter_new_ids = AsyncMock(return_value={"111"})
        mock_smart_deal_radar.evaluate = AsyncMock(return_value=deal)

        with patch.object(
            patrol_engine._scanner, "sweep_category", new_callable=AsyncMock
        ) as mock_sweep, patch.object(
            patrol_engine._interceptor, "start", new_callable=AsyncMock,
        ), patch.object(
            patrol_engine._interceptor, "drain", return_value=[],
        ), patch.object(
            patrol_engine._interceptor, "stop", new_callable=AsyncMock,
        ):
            mock_sweep.return_value = [listing]
            await patrol_engine.run_patrol_cycle()

        mock_notifier.send_deal.assert_called()

    @pytest.mark.asyncio
    async def test_no_notification_when_no_deals(
        self, patrol_engine, mock_listing_repo, mock_notifier,
    ):
        mock_listing_repo.filter_new_ids = AsyncMock(return_value={"111"})

        with patch.object(
            patrol_engine._scanner, "sweep_category", new_callable=AsyncMock
        ) as mock_sweep, patch.object(
            patrol_engine._interceptor, "start", new_callable=AsyncMock,
        ), patch.object(
            patrol_engine._interceptor, "drain", return_value=[],
        ), patch.object(
            patrol_engine._interceptor, "stop", new_callable=AsyncMock,
        ):
            mock_sweep.return_value = [_make_listing("111")]
            await patrol_engine.run_patrol_cycle()

        mock_notifier.send_deal.assert_not_called()


# --- Shadow Ban Detection ---


class TestShadowBanDetection:
    @pytest.mark.asyncio
    async def test_shadow_ban_detected_after_consecutive_empty(self, patrol_engine, mock_listing_repo):
        """3 consecutive empty sweeps should trigger shadow ban warning."""
        mock_listing_repo.filter_new_ids = AsyncMock(return_value=set())

        with patch.object(
            patrol_engine._scanner, "sweep_category", new_callable=AsyncMock
        ) as mock_sweep, patch.object(
            patrol_engine._interceptor, "start", new_callable=AsyncMock,
        ), patch.object(
            patrol_engine._interceptor, "drain", return_value=[],
        ), patch.object(
            patrol_engine._interceptor, "stop", new_callable=AsyncMock,
        ):
            mock_sweep.return_value = []

            # Run 3 empty cycles
            for _ in range(3):
                result = await patrol_engine.run_patrol_cycle()

        assert result.suspected_shadow_ban is True

    @pytest.mark.asyncio
    async def test_no_shadow_ban_with_data(self, patrol_engine, mock_listing_repo):
        """Normal cycles with data should not flag shadow ban."""
        mock_listing_repo.filter_new_ids = AsyncMock(return_value={"111"})

        with patch.object(
            patrol_engine._scanner, "sweep_category", new_callable=AsyncMock
        ) as mock_sweep, patch.object(
            patrol_engine._interceptor, "start", new_callable=AsyncMock,
        ), patch.object(
            patrol_engine._interceptor, "drain", return_value=[],
        ), patch.object(
            patrol_engine._interceptor, "stop", new_callable=AsyncMock,
        ):
            mock_sweep.return_value = [_make_listing("111")]
            result = await patrol_engine.run_patrol_cycle()

        assert result.suspected_shadow_ban is False


# --- Scan Logging ---


class TestScanLogging:
    @pytest.mark.asyncio
    async def test_logs_patrol_cycle(self, patrol_engine, mock_scan_log_repo, mock_listing_repo):
        mock_listing_repo.filter_new_ids = AsyncMock(return_value=set())

        with patch.object(
            patrol_engine._scanner, "sweep_category", new_callable=AsyncMock
        ) as mock_sweep, patch.object(
            patrol_engine._interceptor, "start", new_callable=AsyncMock,
        ), patch.object(
            patrol_engine._interceptor, "drain", return_value=[],
        ), patch.object(
            patrol_engine._interceptor, "stop", new_callable=AsyncMock,
        ):
            mock_sweep.return_value = []
            await patrol_engine.run_patrol_cycle()

        mock_scan_log_repo.save.assert_called_once()
        log = mock_scan_log_repo.save.call_args[0][0]
        assert isinstance(log, ScanLog)
        assert log.site == "facebook_marketplace"
        assert log.category == "patrol"


# --- Full Cycle Result ---


class TestPatrolCycleResult:
    @pytest.mark.asyncio
    async def test_returns_result_dataclass(self, patrol_engine, mock_listing_repo):
        from agentic_scraper.scanner.patrol_engine import PatrolCycleResult

        mock_listing_repo.filter_new_ids = AsyncMock(return_value=set())

        with patch.object(
            patrol_engine._scanner, "sweep_category", new_callable=AsyncMock
        ) as mock_sweep, patch.object(
            patrol_engine._interceptor, "start", new_callable=AsyncMock,
        ), patch.object(
            patrol_engine._interceptor, "drain", return_value=[],
        ), patch.object(
            patrol_engine._interceptor, "stop", new_callable=AsyncMock,
        ):
            mock_sweep.return_value = []
            result = await patrol_engine.run_patrol_cycle()

        assert isinstance(result, PatrolCycleResult)
        assert result.categories_swept >= 0
        assert isinstance(result.new_listings, int)
        assert isinstance(result.deals_found, int)
        assert isinstance(result.data_source, str)
        assert isinstance(result.suspected_shadow_ban, bool)

    @pytest.mark.asyncio
    async def test_result_counts_correct(
        self, patrol_engine, mock_listing_repo, mock_smart_deal_radar,
    ):
        listing = _make_listing("111")
        deal = Deal(
            listing_id="listing-111",
            score=DealScore.GOOD,
            estimated_market_price=500.0,
            discount_pct=40.0,
        )
        mock_listing_repo.filter_new_ids = AsyncMock(return_value={"111"})
        mock_smart_deal_radar.evaluate = AsyncMock(return_value=deal)

        with patch.object(
            patrol_engine._scanner, "sweep_category", new_callable=AsyncMock
        ) as mock_sweep, patch.object(
            patrol_engine._interceptor, "start", new_callable=AsyncMock,
        ), patch.object(
            patrol_engine._interceptor, "drain", return_value=[],
        ), patch.object(
            patrol_engine._interceptor, "stop", new_callable=AsyncMock,
        ):
            mock_sweep.return_value = [listing]
            result = await patrol_engine.run_patrol_cycle()

        assert result.new_listings >= 1
        assert result.deals_found >= 1


# --- Error Handling ---


class TestErrorHandling:
    @pytest.mark.asyncio
    async def test_sweep_error_doesnt_crash_cycle(self, patrol_engine, mock_listing_repo):
        """If category sweep fails, cycle should still complete."""
        mock_listing_repo.filter_new_ids = AsyncMock(return_value=set())

        with patch.object(
            patrol_engine._scanner, "sweep_category",
            side_effect=Exception("Network error"),
        ), patch.object(
            patrol_engine._interceptor, "start", new_callable=AsyncMock,
        ), patch.object(
            patrol_engine._interceptor, "drain", return_value=[],
        ), patch.object(
            patrol_engine._interceptor, "stop", new_callable=AsyncMock,
        ):
            result = await patrol_engine.run_patrol_cycle()

        assert result is not None

    @pytest.mark.asyncio
    async def test_radar_error_doesnt_crash_cycle(
        self, patrol_engine, mock_listing_repo, mock_smart_deal_radar,
    ):
        """SmartDealRadar failure on one listing shouldn't kill the cycle."""
        mock_listing_repo.filter_new_ids = AsyncMock(return_value={"111"})
        mock_smart_deal_radar.evaluate = AsyncMock(
            side_effect=Exception("LLM timeout")
        )

        with patch.object(
            patrol_engine._scanner, "sweep_category", new_callable=AsyncMock
        ) as mock_sweep, patch.object(
            patrol_engine._interceptor, "start", new_callable=AsyncMock,
        ), patch.object(
            patrol_engine._interceptor, "drain", return_value=[],
        ), patch.object(
            patrol_engine._interceptor, "stop", new_callable=AsyncMock,
        ):
            mock_sweep.return_value = [_make_listing("111")]
            result = await patrol_engine.run_patrol_cycle()

        assert result is not None
        assert result.deals_found == 0
