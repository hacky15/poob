"""Tests for PatrolEngine - the 4-phase GraphQL-first patrol cycle."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from poob.config import AppConfig
from poob.scanner.interest_matcher import InterestMatcher
from poob.storage.models import Deal, DealScore, Listing, ScanLog, WatchItem


# --- Fixtures ---


def _make_listing(
    external_id: str = "111",
    title: str = "PS5 Console",
    price: float = 300.0,
    **kwargs,
) -> Listing:
    # Default to a recent timestamp so listings pass the freshness filter.
    # Tests that specifically test stale filtering can override with posted_at=None.
    if "posted_at" not in kwargs:
        kwargs["posted_at"] = datetime.now(timezone.utc)
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
    discord_user_id: str = "123456789",
) -> WatchItem:
    return WatchItem(
        id=id, interest=interest, max_price=max_price,
        discord_user_id=discord_user_id,
    )


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
        deal_public_min_score="incredible",
        deal_watchlist_min_score="good",
        listing_max_age_hours=6,
        marketplace_default_location="appleton",
        patrol_center_lat=0.0,
        patrol_center_lon=0.0,
        scan_max_listings_per_query=50,
        patrol_anonymous_browse_categories=["electronics"],
    )


@pytest.fixture
def mock_browser_manager():
    mgr = AsyncMock()
    page = AsyncMock()
    mgr.get_page = AsyncMock(return_value=page)
    # get_session() is a sync method that returns a BrowserSession — mock it
    # so that _enrich_listings_from_detail_pages can create extra tabs.
    session = AsyncMock()
    session.new_page = AsyncMock(return_value=AsyncMock())
    session.close_page = AsyncMock()
    mgr.get_session = Mock(return_value=session)
    return mgr


@pytest.fixture
def mock_listing_repo():
    repo = AsyncMock()
    repo.exists = AsyncMock(return_value=False)
    repo.save = AsyncMock(side_effect=lambda listing: listing)
    repo.filter_new_ids = AsyncMock(return_value=set())
    repo.get_known_external_ids = AsyncMock(return_value=set())
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


def _default_vlm_eval():
    """Default VLMEvaluation for mocks."""
    from poob.skills.models import VLMEvaluation
    return VLMEvaluation(
        item_identified="Generic Item",
        condition="good",
        deal_quality="pass",
        confidence=0.8,
        reasoning="No deal detected",
    )


@pytest.fixture
def mock_smart_deal_radar():
    radar = AsyncMock()
    # evaluate_batch returns list[tuple[Deal | None, VLMEvaluation]]
    radar.evaluate_batch = AsyncMock(return_value=[])
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
    from poob.scanner.patrol_engine import PatrolEngine

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


# --- Sweep + Intercept ---


class TestSweepAndIntercept:
    @pytest.mark.asyncio
    async def test_unified_mode_single_sweep(self, patrol_engine, mock_listing_repo):
        """Unified mode should sweep once per cycle (one category from the rotation)."""
        mock_listing_repo.filter_new_ids = AsyncMock(return_value=set())
        mock_listing_repo.get_known_external_ids = AsyncMock(return_value=set())

        with patch.object(
            patrol_engine._scanner, "sweep_category", new_callable=AsyncMock
        ) as mock_sweep, patch.object(
            patrol_engine, "_fetch_anonymous_graphql", new_callable=AsyncMock,
            return_value=[],
        ):
            mock_sweep.return_value = []
            result = await patrol_engine.run_patrol_cycle()

        # Unified mode rotates through one category per cycle — exactly 1 call.
        assert mock_sweep.call_count == 1

    @pytest.mark.asyncio
    async def test_unified_mode_rotates_categories_across_cycles(
        self, patrol_engine, mock_config, mock_listing_repo,
    ):
        """Each successive cycle should pick the next category in the rotation.

        Pre-fix behavior was to always sweep the FB home page (`category=None`),
        which in WI is dominated by vehicles/housing. Rotation diversifies the
        feed across the configured browse categories.
        """
        mock_config.patrol_anonymous_browse_categories = [
            "electronics", "furniture", "appliances",
        ]
        # Reset engine state so the test starts at rotation index 0.
        patrol_engine._dom_category_idx = 0
        mock_listing_repo.filter_new_ids = AsyncMock(return_value=set())
        mock_listing_repo.get_known_external_ids = AsyncMock(return_value=set())

        with patch.object(
            patrol_engine._scanner, "sweep_category", new_callable=AsyncMock
        ) as mock_sweep, patch.object(
            patrol_engine, "_fetch_anonymous_graphql", new_callable=AsyncMock,
            return_value=[],
        ):
            mock_sweep.return_value = []
            for _ in range(4):  # 4 cycles, rotation size 3 → wraps once
                await patrol_engine.run_patrol_cycle()

        # Each call's second positional arg is the category passed to the scanner.
        categories_used = [call.args[1] for call in mock_sweep.call_args_list]
        assert categories_used == [
            "electronics", "furniture", "appliances", "electronics",
        ]

    @pytest.mark.asyncio
    async def test_unified_mode_empty_string_in_rotation_means_home_page(
        self, patrol_engine, mock_config, mock_listing_repo,
    ):
        """Empty string in patrol_anonymous_browse_categories means
        the FB marketplace home page (passed as `None` to sweep_category)."""
        mock_config.patrol_anonymous_browse_categories = ["", "electronics"]
        patrol_engine._dom_category_idx = 0
        mock_listing_repo.filter_new_ids = AsyncMock(return_value=set())
        mock_listing_repo.get_known_external_ids = AsyncMock(return_value=set())

        with patch.object(
            patrol_engine._scanner, "sweep_category", new_callable=AsyncMock
        ) as mock_sweep, patch.object(
            patrol_engine, "_fetch_anonymous_graphql", new_callable=AsyncMock,
            return_value=[],
        ):
            mock_sweep.return_value = []
            await patrol_engine.run_patrol_cycle()  # idx 0 — empty → None
            await patrol_engine.run_patrol_cycle()  # idx 1 — "electronics"

        categories_used = [call.args[1] for call in mock_sweep.call_args_list]
        assert categories_used == [None, "electronics"]

    @pytest.mark.asyncio
    async def test_categories_mode_sweeps_all(self, patrol_engine, mock_config, mock_listing_repo):
        """Categories mode should sweep each configured category + all."""
        mock_config.patrol_sweep_mode = "categories"
        patrol_engine._sweep_mode = "categories"
        mock_listing_repo.filter_new_ids = AsyncMock(return_value=set())
        mock_listing_repo.get_known_external_ids = AsyncMock(return_value=set())

        with patch.object(
            patrol_engine._scanner, "sweep_category", new_callable=AsyncMock
        ) as mock_sweep, patch.object(
            patrol_engine, "_fetch_anonymous_graphql", new_callable=AsyncMock,
            return_value=[],
        ):
            mock_sweep.return_value = []
            result = await patrol_engine.run_patrol_cycle()

        # electronics + furniture + all(None) = 3 calls
        assert mock_sweep.call_count == 3

    @pytest.mark.asyncio
    async def test_graphql_data_preferred_over_dom(self, patrol_engine, mock_listing_repo):
        """When JS interceptor captures GraphQL responses, that data should be primary."""
        from poob.scanner.patrol_engine import PatrolCycleResult

        page = await patrol_engine._browser.get_page()

        # The JS drain returns captured GraphQL response bodies
        gql_json = json.dumps({
            "data": {
                "marketplace_search": {
                    "feed_units": {
                        "edges": [{
                            "node": {
                                "listing": {
                                    "id": "9991110001",
                                    "marketplace_listing_title": "PS5 from GraphQL",
                                    "listing_price": {"amount": "250.00", "currency": "USD"},
                                    "creation_time": int(time.time()),
                                }
                            }
                        }]
                    }
                }
            }
        })

        # page.evaluate returns different things depending on the script:
        # - inject script: None
        # - drain script: list of captured JSON bodies
        # - sweep also calls evaluate for DOM extraction (handled by sweep mock)
        call_count = {"n": 0}

        async def mock_evaluate(script):
            call_count["n"] += 1
            # Drain call returns captured GraphQL bodies
            if "__gql_captures" in script and "return" in script:
                return [gql_json]
            return None

        page.evaluate = AsyncMock(side_effect=mock_evaluate)

        mock_listing_repo.filter_new_ids = AsyncMock(return_value={"9991110001"})
        result = PatrolCycleResult()

        with patch.object(
            patrol_engine, "_sweep_categories", new_callable=AsyncMock,
            return_value=[],  # No DOM listings
        ), patch.object(
            patrol_engine, "_fetch_anonymous_graphql", new_callable=AsyncMock,
            return_value=[],  # Isolate test to JS intercept path
        ):
            listings, data_source = await patrol_engine._sweep_and_intercept(page, result)

        assert data_source in ("graphql", "anonymous_graphql")
        assert len(listings) == 1
        assert listings[0].title == "PS5 from GraphQL"
        assert listings[0].posted_at is not None

    @pytest.mark.asyncio
    async def test_dom_fallback_when_graphql_empty(self, patrol_engine, mock_listing_repo):
        """When no GraphQL responses are captured, DOM data should be used."""
        dom_listing = _make_listing("dom-111", "Table from DOM", 50.0)
        mock_listing_repo.filter_new_ids = AsyncMock(return_value={"dom-111"})
        mock_listing_repo.get_known_external_ids = AsyncMock(return_value=set())

        with patch.object(
            patrol_engine._scanner, "sweep_category", new_callable=AsyncMock
        ) as mock_sweep, patch.object(
            patrol_engine, "_fetch_anonymous_graphql", new_callable=AsyncMock,
            return_value=[],
        ):
            mock_sweep.return_value = [dom_listing]
            result = await patrol_engine.run_patrol_cycle()

        assert result.data_source == "dom_fallback"
        assert result.new_listings == 1

    @pytest.mark.asyncio
    async def test_empty_graphql_responses_ignored(self, patrol_engine, mock_listing_repo):
        """Non-marketplace GraphQL responses should be silently ignored."""
        dom_listing = _make_listing("111")
        mock_listing_repo.filter_new_ids = AsyncMock(return_value={"111"})
        mock_listing_repo.get_known_external_ids = AsyncMock(return_value=set())

        with patch.object(
            patrol_engine._scanner, "sweep_category", new_callable=AsyncMock
        ) as mock_sweep, patch.object(
            patrol_engine, "_fetch_anonymous_graphql", new_callable=AsyncMock,
            return_value=[],
        ):
            mock_sweep.return_value = [dom_listing]
            result = await patrol_engine.run_patrol_cycle()

        assert result.data_source == "dom_fallback"
        assert result.new_listings == 1


# --- Batch Dedup ---


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
            patrol_engine, "_fetch_anonymous_graphql", new_callable=AsyncMock,
            return_value=[],
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
            patrol_engine, "_fetch_anonymous_graphql", new_callable=AsyncMock,
            return_value=[],
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
            patrol_engine, "_fetch_anonymous_graphql", new_callable=AsyncMock,
            return_value=[],
        ):
            mock_sweep.return_value = listings
            result = await patrol_engine.run_patrol_cycle()

        # Should still save via individual fallback
        assert mock_listing_repo.save.call_count == 1


# --- Evaluation ---


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
            patrol_engine, "_fetch_anonymous_graphql", new_callable=AsyncMock,
            return_value=[],
        ):
            mock_sweep.return_value = listings
            await patrol_engine.run_patrol_cycle()

        assert mock_smart_deal_radar.evaluate_batch.call_count >= 1

    @pytest.mark.asyncio
    async def test_watchlist_deal_from_pipeline(
        self,
        patrol_engine,
        mock_listing_repo,
        mock_watchlist_repo,
        mock_smart_deal_radar,
        mock_deal_repo,
        mock_notifier,
    ):
        """Pipeline returns watchlist deals which get DM'd."""
        listing = _make_listing("111", title="PS5 Console Bundle", price=300.0)
        interest = _make_interest("watch-1", "PS5", 500.0)

        watchlist_deal = Deal(
            listing_id="listing-111",
            watch_item_id="watch-1",
            score=DealScore.GOOD,
            estimated_market_price=450.0,
            discount_pct=33.0,
            llm_reasoning="Good deal on PS5",
        )

        mock_listing_repo.filter_new_ids = AsyncMock(return_value={"111"})
        mock_watchlist_repo.list_active = AsyncMock(return_value=[interest])
        mock_watchlist_repo.get = AsyncMock(return_value=interest)
        mock_smart_deal_radar.evaluate_batch = AsyncMock(
            return_value=[(watchlist_deal, _default_vlm_eval())]
        )

        with patch.object(
            patrol_engine._scanner, "sweep_category", new_callable=AsyncMock
        ) as mock_sweep, patch.object(
            patrol_engine, "_fetch_anonymous_graphql", new_callable=AsyncMock,
            return_value=[],
        ):
            mock_sweep.return_value = [listing]
            await patrol_engine.run_patrol_cycle()

        assert mock_deal_repo.save.call_count >= 1
        mock_notifier.send_deal_dm.assert_called()

    @pytest.mark.asyncio
    async def test_radar_returns_base_and_watchlist_deals(
        self,
        patrol_engine,
        mock_listing_repo,
        mock_watchlist_repo,
        mock_smart_deal_radar,
        mock_deal_repo,
        mock_notifier,
    ):
        """Pipeline can return both base deals and watchlist deals."""
        listing1 = _make_listing("111", title="PS5 Console", price=200.0)
        listing2 = _make_listing("222", title="Nice Table", price=30.0)
        interest = _make_interest("watch-1", "PS5", 500.0)

        base_deal = Deal(
            listing_id="listing-222",
            score=DealScore.INCREDIBLE,
            estimated_market_price=200.0,
            discount_pct=85.0,
            llm_reasoning="Incredible table deal",
        )
        watchlist_deal = Deal(
            listing_id="listing-111",
            watch_item_id="watch-1",
            score=DealScore.GREAT,
            estimated_market_price=450.0,
            discount_pct=55.5,
            llm_reasoning="Great deal on PS5",
        )

        mock_listing_repo.filter_new_ids = AsyncMock(return_value={"111", "222"})
        mock_watchlist_repo.list_active = AsyncMock(return_value=[interest])
        mock_watchlist_repo.get = AsyncMock(return_value=interest)
        mock_smart_deal_radar.evaluate_batch = AsyncMock(
            return_value=[
                (watchlist_deal, _default_vlm_eval()),
                (base_deal, _default_vlm_eval()),
            ]
        )

        with patch.object(
            patrol_engine._scanner, "sweep_category", new_callable=AsyncMock
        ) as mock_sweep, patch.object(
            patrol_engine, "_fetch_anonymous_graphql", new_callable=AsyncMock,
            return_value=[],
        ):
            mock_sweep.return_value = [listing1, listing2]
            await patrol_engine.run_patrol_cycle()

        assert mock_deal_repo.save.call_count >= 2
        mock_notifier.send_deal.assert_called()
        mock_notifier.send_deal_dm.assert_called()


# --- Notify ---


class TestNotify:
    @pytest.mark.asyncio
    async def test_public_channel_notifies_incredible_deals(
        self,
        patrol_engine,
        mock_listing_repo,
        mock_smart_deal_radar,
        mock_notifier,
        mock_deal_repo,
    ):
        """INCREDIBLE base deals go to the public channel."""
        listing = _make_listing("111")
        deal = Deal(
            listing_id="listing-111",
            score=DealScore.INCREDIBLE,
            estimated_market_price=500.0,
            discount_pct=70.0,
            llm_reasoning="Incredible deal",
        )
        mock_listing_repo.filter_new_ids = AsyncMock(return_value={"111"})
        mock_smart_deal_radar.evaluate_batch = AsyncMock(
            return_value=[(deal, _default_vlm_eval())]
        )

        with patch.object(
            patrol_engine._scanner, "sweep_category", new_callable=AsyncMock
        ) as mock_sweep, patch.object(
            patrol_engine, "_fetch_anonymous_graphql", new_callable=AsyncMock,
            return_value=[],
        ):
            mock_sweep.return_value = [listing]
            await patrol_engine.run_patrol_cycle()

        mock_notifier.send_deal.assert_called()

    @pytest.mark.asyncio
    async def test_public_channel_skips_good_deals(
        self,
        patrol_engine,
        mock_listing_repo,
        mock_smart_deal_radar,
        mock_notifier,
        mock_deal_repo,
    ):
        """GOOD base deals should NOT go to the public channel (requires INCREDIBLE)."""
        listing = _make_listing("111", title="Random Item", price=50.0)
        deal = Deal(
            listing_id="listing-111",
            score=DealScore.GOOD,
            estimated_market_price=100.0,
            discount_pct=50.0,
        )
        mock_listing_repo.filter_new_ids = AsyncMock(return_value={"111"})
        mock_smart_deal_radar.evaluate_batch = AsyncMock(
            return_value=[(deal, _default_vlm_eval())]
        )

        with patch.object(
            patrol_engine._scanner, "sweep_category", new_callable=AsyncMock
        ) as mock_sweep, patch.object(
            patrol_engine, "_fetch_anonymous_graphql", new_callable=AsyncMock,
            return_value=[],
        ):
            mock_sweep.return_value = [listing]
            await patrol_engine.run_patrol_cycle()

        mock_notifier.send_deal.assert_not_called()

    @pytest.mark.asyncio
    async def test_watchlist_dm_sent_for_good_deal(
        self,
        patrol_engine,
        mock_listing_repo,
        mock_watchlist_repo,
        mock_smart_deal_radar,
        mock_notifier,
        mock_deal_repo,
    ):
        """GOOD watchlist deals should be DM'd to the user who owns the watch item."""
        listing = _make_listing("111", title="PS5 Console Bundle", price=300.0)
        interest = _make_interest("watch-1", "PS5", 500.0, discord_user_id="987654321")

        watchlist_deal = Deal(
            listing_id="listing-111",
            watch_item_id="watch-1",
            score=DealScore.GOOD,
            estimated_market_price=450.0,
            discount_pct=33.0,
            llm_reasoning="Good deal on PS5",
        )
        mock_listing_repo.filter_new_ids = AsyncMock(return_value={"111"})
        mock_watchlist_repo.list_active = AsyncMock(return_value=[interest])
        mock_watchlist_repo.get = AsyncMock(return_value=interest)
        mock_smart_deal_radar.evaluate_batch = AsyncMock(
            return_value=[(watchlist_deal, _default_vlm_eval())]
        )

        with patch.object(
            patrol_engine._scanner, "sweep_category", new_callable=AsyncMock
        ) as mock_sweep, patch.object(
            patrol_engine, "_fetch_anonymous_graphql", new_callable=AsyncMock,
            return_value=[],
        ):
            mock_sweep.return_value = [listing]
            await patrol_engine.run_patrol_cycle()

        mock_notifier.send_deal_dm.assert_called()
        call_kwargs = mock_notifier.send_deal_dm.call_args
        assert call_kwargs[1]["discord_user_id"] == 987654321
        assert call_kwargs[1]["watch_interest"] == "PS5"

    @pytest.mark.asyncio
    async def test_no_notification_when_no_deals(
        self, patrol_engine, mock_listing_repo, mock_notifier,
    ):
        mock_listing_repo.filter_new_ids = AsyncMock(return_value={"111"})

        with patch.object(
            patrol_engine._scanner, "sweep_category", new_callable=AsyncMock
        ) as mock_sweep, patch.object(
            patrol_engine, "_fetch_anonymous_graphql", new_callable=AsyncMock,
            return_value=[],
        ):
            mock_sweep.return_value = [_make_listing("111")]
            await patrol_engine.run_patrol_cycle()

        mock_notifier.send_deal.assert_not_called()
        mock_notifier.send_deal_dm.assert_not_called()


# --- Shadow Ban Detection ---


class TestShadowBanDetection:
    @pytest.mark.asyncio
    async def test_shadow_ban_detected_after_consecutive_empty(self, patrol_engine, mock_listing_repo):
        """3 consecutive empty sweeps should trigger shadow ban warning."""
        mock_listing_repo.filter_new_ids = AsyncMock(return_value=set())

        with patch.object(
            patrol_engine._scanner, "sweep_category", new_callable=AsyncMock
        ) as mock_sweep, patch.object(
            patrol_engine, "_fetch_anonymous_graphql", new_callable=AsyncMock,
            return_value=[],
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
            patrol_engine, "_fetch_anonymous_graphql", new_callable=AsyncMock,
            return_value=[],
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
            patrol_engine, "_fetch_anonymous_graphql", new_callable=AsyncMock,
            return_value=[],
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
        from poob.scanner.patrol_engine import PatrolCycleResult

        mock_listing_repo.filter_new_ids = AsyncMock(return_value=set())

        with patch.object(
            patrol_engine._scanner, "sweep_category", new_callable=AsyncMock
        ) as mock_sweep, patch.object(
            patrol_engine, "_fetch_anonymous_graphql", new_callable=AsyncMock,
            return_value=[],
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
            score=DealScore.INCREDIBLE,
            estimated_market_price=500.0,
            discount_pct=70.0,
        )
        mock_listing_repo.filter_new_ids = AsyncMock(return_value={"111"})
        mock_smart_deal_radar.evaluate_batch = AsyncMock(
            return_value=[(deal, _default_vlm_eval())]
        )

        with patch.object(
            patrol_engine._scanner, "sweep_category", new_callable=AsyncMock
        ) as mock_sweep, patch.object(
            patrol_engine, "_fetch_anonymous_graphql", new_callable=AsyncMock,
            return_value=[],
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
            patrol_engine, "_fetch_anonymous_graphql", new_callable=AsyncMock,
            return_value=[],
        ):
            result = await patrol_engine.run_patrol_cycle()

        assert result is not None

    @pytest.mark.asyncio
    async def test_radar_error_doesnt_crash_cycle(
        self, patrol_engine, mock_listing_repo, mock_smart_deal_radar,
    ):
        """SmartDealRadar batch failure shouldn't kill the cycle."""
        mock_listing_repo.filter_new_ids = AsyncMock(return_value={"111"})
        mock_smart_deal_radar.evaluate_batch = AsyncMock(
            side_effect=Exception("LLM timeout")
        )

        with patch.object(
            patrol_engine._scanner, "sweep_category", new_callable=AsyncMock
        ) as mock_sweep, patch.object(
            patrol_engine, "_fetch_anonymous_graphql", new_callable=AsyncMock,
            return_value=[],
        ):
            mock_sweep.return_value = [_make_listing("111")]
            result = await patrol_engine.run_patrol_cycle()

        assert result is not None
        assert result.deals_found == 0


# --- Stale Listing Filtering ---


# TestStaleFiltering was removed — all filter tests are now in
# test_listing_filter.py which tests the unified FilterChain
# (SponsoredFilter, FreshnessFilter, CategoryFilter, GeoDistanceFilter, GarbageFilter).


# --- Sort Order Verification ---


class TestSortVerification:
    def test_sorted_listings_no_warning(self, patrol_engine):
        """Properly sorted listings should not trigger a warning."""
        now = datetime.now(timezone.utc)
        listings = [
            _make_listing("1", posted_at=now - timedelta(minutes=5)),
            _make_listing("2", posted_at=now - timedelta(minutes=10)),
        ]
        patrol_engine._verify_sort_order(listings)  # should not raise

    def test_unsorted_listings_warns(self, patrol_engine, capsys):
        """Out-of-order timestamps should produce a warning log."""
        now = datetime.now(timezone.utc)
        listings = [
            _make_listing("1", posted_at=now - timedelta(hours=3)),  # older
            _make_listing("2", posted_at=now - timedelta(minutes=5)),  # newer = violation
        ]
        patrol_engine._verify_sort_order(listings)
        captured = capsys.readouterr()
        assert "Sort order verification failed" in captured.out

    def test_no_timestamps_skips_check(self, patrol_engine):
        """Listings without timestamps should be silently skipped."""
        listings = [
            _make_listing("1", posted_at=None),
            _make_listing("2", posted_at=None),
        ]
        patrol_engine._verify_sort_order(listings)  # should not raise

    def test_single_listing_skips_check(self, patrol_engine):
        """Single listing cannot violate sort order."""
        now = datetime.now(timezone.utc)
        listings = [_make_listing("1", posted_at=now)]
        patrol_engine._verify_sort_order(listings)  # should not raise


# --- GraphQL Timestamp Parsing ---


class TestGraphQLTimestampParsing:
    def test_creation_time_parsed_to_posted_at(self):
        """GraphQL creation_time should be parsed into Listing.posted_at."""
        from poob.browser.graphql_interceptor import GraphQLListingData
        from poob.scanner.patrol_engine import _graphql_to_listing

        gql = GraphQLListingData(
            external_id="123",
            title="Test Item",
            price=100.0,
            posted_at="1709337600",  # 2024-03-02 00:00:00 UTC
        )
        listing = _graphql_to_listing(gql)
        assert listing.posted_at is not None
        assert listing.posted_at.year >= 2024

    def test_invalid_creation_time_sets_none(self):
        """Invalid creation_time should result in posted_at=None."""
        from poob.browser.graphql_interceptor import GraphQLListingData
        from poob.scanner.patrol_engine import _graphql_to_listing

        gql = GraphQLListingData(
            external_id="123",
            title="Test Item",
            posted_at="not-a-number",
        )
        listing = _graphql_to_listing(gql)
        assert listing.posted_at is None

    def test_no_creation_time_sets_none(self):
        """Missing creation_time should result in posted_at=None."""
        from poob.browser.graphql_interceptor import GraphQLListingData
        from poob.scanner.patrol_engine import _graphql_to_listing

        gql = GraphQLListingData(external_id="123", title="Test Item")
        listing = _graphql_to_listing(gql)
        assert listing.posted_at is None


# --- Freshness Text Parsing ---


class TestFreshnessParsing:
    def test_just_listed(self):
        from poob.sites.facebook.parser import _parse_freshness

        result = _parse_freshness("Just listed")
        assert result is not None
        assert (datetime.now(timezone.utc) - result).total_seconds() < 5

    def test_minutes_ago(self):
        from poob.sites.facebook.parser import _parse_freshness

        result = _parse_freshness("Listed 30 minutes ago")
        assert result is not None
        diff = (datetime.now(timezone.utc) - result).total_seconds()
        assert 1700 < diff < 1900  # ~30 minutes

    def test_hours_ago(self):
        from poob.sites.facebook.parser import _parse_freshness

        result = _parse_freshness("Listed 2 hours ago")
        assert result is not None
        diff = (datetime.now(timezone.utc) - result).total_seconds()
        assert 7000 < diff < 7400  # ~2 hours

    def test_yesterday(self):
        from poob.sites.facebook.parser import _parse_freshness

        result = _parse_freshness("Listed yesterday")
        assert result is not None
        diff = (datetime.now(timezone.utc) - result).total_seconds()
        assert 85000 < diff < 87000  # ~24 hours

    def test_days_ago(self):
        from poob.sites.facebook.parser import _parse_freshness

        result = _parse_freshness("Listed 3 days ago")
        assert result is not None
        diff = (datetime.now(timezone.utc) - result).total_seconds()
        assert 258000 < diff < 260000  # ~3 days

    def test_empty_string(self):
        from poob.sites.facebook.parser import _parse_freshness

        assert _parse_freshness("") is None

    def test_unknown_format(self):
        from poob.sites.facebook.parser import _parse_freshness

        assert _parse_freshness("Some random text") is None


# --- Watchlist Exemption from Stale Filter ---


class TestWatchlistExemption:
    @pytest.mark.asyncio
    async def test_watchlist_match_survives_no_timestamp(
        self,
        patrol_engine,
        mock_listing_repo,
        mock_watchlist_repo,
        mock_smart_deal_radar,
        mock_deal_repo,
        mock_notifier,
    ):
        """Watchlist-matched listings need timestamps to be notified (freshness gate)."""
        # Listing needs a recent timestamp — the minute-level freshness gate
        # in _notify blocks listings older than
        # watchlist_notification_max_age_minutes (default 30).
        listing = _make_listing("111", title="PS5 Console Bundle", price=300.0,
                                posted_at=datetime.now(timezone.utc) - timedelta(minutes=5))
        interest = _make_interest("watch-1", "PS5", 500.0, discord_user_id="987654321")

        watchlist_deal = Deal(
            listing_id="listing-111",
            watch_item_id="watch-1",
            score=DealScore.GOOD,
            estimated_market_price=450.0,
            discount_pct=33.0,
            llm_reasoning="Good deal on PS5",
        )

        mock_listing_repo.filter_new_ids = AsyncMock(return_value={"111"})
        mock_watchlist_repo.list_active = AsyncMock(return_value=[interest])
        mock_watchlist_repo.get = AsyncMock(return_value=interest)
        mock_smart_deal_radar.evaluate_batch = AsyncMock(
            return_value=[(watchlist_deal, _default_vlm_eval())]
        )

        with patch.object(
            patrol_engine._scanner, "sweep_category", new_callable=AsyncMock
        ) as mock_sweep, patch.object(
            patrol_engine, "_fetch_anonymous_graphql", new_callable=AsyncMock,
            return_value=[],
        ):
            mock_sweep.return_value = [listing]
            result = await patrol_engine.run_patrol_cycle()

        # The listing should NOT be discarded — it matches watchlist interest "PS5"
        mock_notifier.send_deal_dm.assert_called()
        call_kwargs = mock_notifier.send_deal_dm.call_args
        assert call_kwargs[1]["discord_user_id"] == 987654321

    @pytest.mark.asyncio
    async def test_non_matching_no_timestamp_still_discarded(
        self,
        patrol_engine,
        mock_listing_repo,
        mock_watchlist_repo,
        mock_smart_deal_radar,
        mock_notifier,
    ):
        """No-timestamp listings that DON'T match watchlist should still be discarded."""
        listing = _make_listing("111", title="Random Couch", price=50.0, posted_at=None)
        interest = _make_interest("watch-1", "PS5", 500.0)

        mock_listing_repo.filter_new_ids = AsyncMock(return_value={"111"})
        mock_watchlist_repo.list_active = AsyncMock(return_value=[interest])
        mock_smart_deal_radar.evaluate_batch = AsyncMock(return_value=[])

        with patch.object(
            patrol_engine._scanner, "sweep_category", new_callable=AsyncMock
        ) as mock_sweep, patch.object(
            patrol_engine, "_fetch_anonymous_graphql", new_callable=AsyncMock,
            return_value=[],
        ):
            mock_sweep.return_value = [listing]
            result = await patrol_engine.run_patrol_cycle()

        # "Random Couch" doesn't match "PS5" → no exemption → discarded → no DM
        mock_notifier.send_deal_dm.assert_not_called()
        mock_notifier.send_deal.assert_not_called()

    @pytest.mark.asyncio
    async def test_mixed_watchlist_and_normal_listings(
        self,
        patrol_engine,
        mock_listing_repo,
        mock_watchlist_repo,
        mock_smart_deal_radar,
        mock_deal_repo,
        mock_notifier,
    ):
        """Mix of timestamped and no-timestamp listings. Freshness gate blocks NO_TS from notification."""
        now = datetime.now(timezone.utc)
        # Fresh listing with timestamp (passes filter normally)
        fresh = _make_listing("111", title="Table", price=50.0, posted_at=now)
        # Watchlist match with a RECENT timestamp — must be within the
        # minute-level DM cutoff (watchlist_notification_max_age_minutes,
        # default 30) so the _notify freshness gate passes.
        exempt = _make_listing("222", title="PS5 Disc Edition", price=250.0,
                                posted_at=now - timedelta(minutes=5))
        # No timestamp, doesn't match watchlist (should be discarded)
        discard = _make_listing("333", title="Random Junk", price=10.0, posted_at=None)

        interest = _make_interest("watch-1", "PS5", 500.0, discord_user_id="123")

        watchlist_deal = Deal(
            listing_id="listing-222",
            watch_item_id="watch-1",
            score=DealScore.GOOD,
            estimated_market_price=400.0,
            discount_pct=37.5,
            llm_reasoning="Good deal on PS5",
        )

        mock_listing_repo.filter_new_ids = AsyncMock(return_value={"111", "222", "333"})
        mock_watchlist_repo.list_active = AsyncMock(return_value=[interest])
        mock_watchlist_repo.get = AsyncMock(return_value=interest)
        mock_smart_deal_radar.evaluate_batch = AsyncMock(
            return_value=[(watchlist_deal, _default_vlm_eval())]
        )

        with patch.object(
            patrol_engine._scanner, "sweep_category", new_callable=AsyncMock
        ) as mock_sweep, patch.object(
            patrol_engine, "_fetch_anonymous_graphql", new_callable=AsyncMock,
            return_value=[],
        ):
            mock_sweep.return_value = [fresh, exempt, discard]
            result = await patrol_engine.run_patrol_cycle()

        # 3 new listings, but only 2 survive the stale filter (fresh + exempt)
        # The PS5 exempt listing should trigger a watchlist DM
        mock_notifier.send_deal_dm.assert_called()


class TestWatchlistSweepMultiConfig:
    """Test _sweep_watchlist_items iterates over search_configs per WatchItem."""

    @pytest.mark.asyncio
    async def test_empty_search_configs_uses_single_default(
        self, patrol_engine, mock_watchlist_repo
    ):
        """When search_configs is empty, one sweep_search call with global defaults."""
        item = _make_interest(interest="TV", max_price=200.0)
        item.search_configs = []
        mock_watchlist_repo.list_active = AsyncMock(return_value=[item])

        from poob.scanner.patrol_engine import PatrolCycleResult

        with patch.object(
            patrol_engine._scanner, "sweep_search", new_callable=AsyncMock
        ) as mock_sweep, patch.object(
            patrol_engine._graphql_client, "search_all_pages", new_callable=AsyncMock,
            return_value=[],
        ):
            mock_sweep.return_value = [_make_listing("111", title="TV")]
            result = PatrolCycleResult()
            listings = await patrol_engine._sweep_watchlist_items(MagicMock(), result)

            assert len(listings) == 1
            mock_sweep.assert_called_once()
            call_kwargs = mock_sweep.call_args
            assert call_kwargs[1]["max_price"] == 200.0
            # Default location from config when search_configs is empty
            assert call_kwargs[1]["location_slug"] in (None, "appleton")
            assert call_kwargs[1]["condition"] is None

    @pytest.mark.asyncio
    async def test_multi_config_runs_multiple_searches(
        self, patrol_engine, mock_watchlist_repo
    ):
        """Each search_config entry triggers a separate sweep_search call."""
        item = _make_interest(interest="couch", max_price=300.0)
        item.search_configs = [
            {"location": "madison", "radius_miles": 20, "condition": "used_good"},
            {"location": "appleton", "radius_miles": 40, "max_price": 0},
        ]
        mock_watchlist_repo.list_active = AsyncMock(return_value=[item])

        from poob.scanner.patrol_engine import PatrolCycleResult

        with patch.object(
            patrol_engine._scanner, "sweep_search", new_callable=AsyncMock
        ) as mock_sweep, patch.object(
            patrol_engine._graphql_client, "search_all_pages", new_callable=AsyncMock,
            return_value=[],
        ):
            mock_sweep.side_effect = [
                [_make_listing("111", title="Couch Madison")],
                [_make_listing("222", title="Couch Appleton")],
            ]
            result = PatrolCycleResult()
            listings = await patrol_engine._sweep_watchlist_items(MagicMock(), result)

            assert len(listings) == 2
            assert mock_sweep.call_count == 2

            # Check first call
            first_call = mock_sweep.call_args_list[0]
            assert first_call[1]["location_slug"] == "madison"
            assert first_call[1]["radius_miles"] == 20
            assert first_call[1]["condition"] == "used_good"
            assert first_call[1]["max_price"] == 300.0  # from item.max_price

            # Check second call
            second_call = mock_sweep.call_args_list[1]
            assert second_call[1]["location_slug"] == "appleton"
            assert second_call[1]["radius_miles"] == 40
            assert second_call[1]["max_price"] == 0  # overridden by config

    @pytest.mark.asyncio
    async def test_dedup_across_configs(
        self, patrol_engine, mock_watchlist_repo
    ):
        """Same listing from overlapping searches should not be duplicated."""
        item = _make_interest(interest="desk")
        item.search_configs = [
            {"location": "madison", "radius_miles": 20},
            {"location": "appleton", "radius_miles": 40},
        ]
        mock_watchlist_repo.list_active = AsyncMock(return_value=[item])
        same_listing = _make_listing("111", title="Desk")

        from poob.scanner.patrol_engine import PatrolCycleResult

        with patch.object(
            patrol_engine._scanner, "sweep_search", new_callable=AsyncMock
        ) as mock_sweep, patch.object(
            patrol_engine._graphql_client, "search_all_pages", new_callable=AsyncMock,
            return_value=[],
        ):
            mock_sweep.side_effect = [[same_listing], [same_listing]]
            result = PatrolCycleResult()
            listings = await patrol_engine._sweep_watchlist_items(MagicMock(), result)

            assert len(listings) == 1  # deduped


class TestBrowsePathLocationThreading:
    """The general-browse GQL path must center queries on the configured
    location. A prior bug (audited 2026-05-29) omitted location_slug, so
    build_search_params fell through to a hardcoded Appleton default and FB
    served Fox-Valley inventory ~100mi outside the configured Madison radius
    — which the geo filter then discarded after it flooded the eval pool.
    The watchlist path already threaded location; the browse path didn't."""

    @pytest.mark.asyncio
    async def test_browse_threads_configured_location(self, patrol_engine, mock_config):
        from poob.scanner.patrol_engine import PatrolCycleResult

        mock_config.marketplace_default_location = "madison"
        mock_config.patrol_anonymous_browse_categories = ["electronics", "furniture"]
        patrol_engine._graphql_enabled = True

        with patch.object(
            patrol_engine._graphql_client, "search_all_pages",
            new_callable=AsyncMock, return_value=[],
        ), patch(
            "poob.scanner.patrol_engine.build_search_params",
        ) as mock_bsp:
            mock_bsp.return_value = MagicMock()
            await patrol_engine._fetch_anonymous_graphql(PatrolCycleResult())

        # One build_search_params call per browse category, each carrying the
        # configured location — NOT falling through to the Appleton default.
        assert mock_bsp.call_count == 2
        for call in mock_bsp.call_args_list:
            assert call.kwargs.get("location_slug") == "madison"


class TestAnonBrowserSelfHeal:
    """The anonymous browser's CDP session can die mid-run and never
    recover — every DOM sweep then times out at 60s. In prod this ran
    524 consecutive timeouts over ~3 days with zero ingestion until a
    manual container restart. The engine must detect repeated timeouts
    and recreate the browser in-process."""

    @pytest.fixture
    def engine_with_anon(self, patrol_engine, mock_config):
        anon = AsyncMock()
        anon.get_page = AsyncMock()
        anon.stop = AsyncMock()
        anon.start = AsyncMock()
        patrol_engine._anonymous_browser = anon
        mock_config.patrol_anonymous_browser_enabled = True
        patrol_engine._anon_sweep_max_timeouts = 3
        patrol_engine._anon_sweep_consecutive_timeouts = 0
        return patrol_engine, anon

    @pytest.mark.asyncio
    async def test_timeout_increments_counter_below_threshold(self, engine_with_anon):
        """Timeouts below the threshold increment the counter but don't restart."""
        import asyncio as _asyncio

        from poob.scanner.patrol_engine import PatrolCycleResult

        engine, _anon = engine_with_anon
        with patch.object(
            engine, "_anon_dom_sweep", new_callable=AsyncMock,
            side_effect=_asyncio.TimeoutError,
        ), patch.object(
            engine, "_fetch_anonymous_graphql", new_callable=AsyncMock, return_value=[],
        ), patch.object(
            engine, "_restart_anonymous_browser", new_callable=AsyncMock,
        ) as mock_restart:
            await engine._sweep_and_intercept(None, PatrolCycleResult())
            await engine._sweep_and_intercept(None, PatrolCycleResult())

        assert engine._anon_sweep_consecutive_timeouts == 2
        mock_restart.assert_not_called()

    @pytest.mark.asyncio
    async def test_restart_triggered_at_threshold(self, engine_with_anon):
        """The Nth consecutive timeout triggers an in-process browser restart."""
        import asyncio as _asyncio

        from poob.scanner.patrol_engine import PatrolCycleResult

        engine, _anon = engine_with_anon
        with patch.object(
            engine, "_anon_dom_sweep", new_callable=AsyncMock,
            side_effect=_asyncio.TimeoutError,
        ), patch.object(
            engine, "_fetch_anonymous_graphql", new_callable=AsyncMock, return_value=[],
        ), patch.object(
            engine, "_restart_anonymous_browser", new_callable=AsyncMock,
        ) as mock_restart:
            for _ in range(3):  # threshold is 3
                await engine._sweep_and_intercept(None, PatrolCycleResult())

        mock_restart.assert_called_once()

    @pytest.mark.asyncio
    async def test_successful_sweep_resets_counter(self, engine_with_anon):
        """A sweep that completes (browser responsive) clears the hang counter."""
        from poob.scanner.patrol_engine import PatrolCycleResult

        engine, _anon = engine_with_anon
        engine._anon_sweep_consecutive_timeouts = 2  # primed near threshold
        with patch.object(
            engine, "_anon_dom_sweep", new_callable=AsyncMock, return_value=[],
        ), patch.object(
            engine, "_fetch_anonymous_graphql", new_callable=AsyncMock, return_value=[],
        ):
            await engine._sweep_and_intercept(None, PatrolCycleResult())

        assert engine._anon_sweep_consecutive_timeouts == 0

    @pytest.mark.asyncio
    async def test_restart_helper_stops_then_starts(self, engine_with_anon):
        """The restart helper tears down then recreates the browser and
        resets the counter on success."""
        engine, anon = engine_with_anon
        engine._anon_sweep_consecutive_timeouts = 5
        await engine._restart_anonymous_browser()
        anon.stop.assert_awaited_once()
        anon.start.assert_awaited_once()
        assert engine._anon_sweep_consecutive_timeouts == 0

    @pytest.mark.asyncio
    async def test_restart_starts_even_if_stop_hangs(self, engine_with_anon):
        """A hung stop() must not block the recreate — start() still runs."""
        engine, anon = engine_with_anon
        anon.stop = AsyncMock(side_effect=Exception("stop hung"))
        await engine._restart_anonymous_browser()
        anon.start.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_restart_leaves_counter_elevated_on_start_failure(self, engine_with_anon):
        """If start() fails, the counter stays elevated so the next cycle retries."""
        engine, anon = engine_with_anon
        engine._anon_sweep_consecutive_timeouts = 4
        anon.start = AsyncMock(side_effect=Exception("start failed"))
        await engine._restart_anonymous_browser()
        # Not reset — next cycle will try again.
        assert engine._anon_sweep_consecutive_timeouts == 4
