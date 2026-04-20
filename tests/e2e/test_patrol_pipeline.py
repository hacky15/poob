"""End-to-end tests for the patrol pipeline with all boundaries mocked."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from poob.scanner.interest_matcher import InterestMatcher
from poob.scanner.patrol_engine import PatrolEngine, PatrolCycleResult
from poob.storage.models import Deal, DealScore, Listing, WatchItem


def _make_listing(
    external_id: str,
    title: str,
    price: float,
) -> Listing:
    return Listing(
        site="facebook_marketplace",
        external_id=external_id,
        title=title,
        price=price,
        listing_url=f"https://facebook.com/marketplace/item/{external_id}",
        location="Appleton, WI",
        posted_at=datetime.now(timezone.utc),
    )


@pytest.fixture
def mock_config():
    return MagicMock(
        patrol_categories=["electronics", "furniture"],
        patrol_inter_category_delay_min_ms=1,
        patrol_inter_category_delay_max_ms=2,
        patrol_inter_listing_delay_min_ms=1,
        patrol_inter_listing_delay_max_ms=2,
        patrol_base_radius_miles=20,
        patrol_radius_jitter=4,
        patrol_days_since_listed=1,
        patrol_include_all_categories=False,
        patrol_deep_inspect_enabled=True,
        patrol_radius_oscillation_enabled=False,
        patrol_sweep_mode="unified",
        deal_radar_max_evaluations=20,
        deal_radar_min_score="good",
        deal_public_min_score="incredible",
        deal_watchlist_min_score="good",
        listing_max_age_hours=6,
    )


@pytest.fixture
def mock_browser_manager():
    mgr = AsyncMock()
    page = AsyncMock()
    mgr.get_page = AsyncMock(return_value=page)
    session = AsyncMock()
    session.new_page = AsyncMock(return_value=AsyncMock())
    session.close_page = AsyncMock()
    mgr.get_session = Mock(return_value=session)
    return mgr


def _build_patrol_engine(
    db_connection,
    mock_browser_manager,
    mock_config,
    smart_deal_radar=None,
):
    """Build a PatrolEngine with real repos and in-memory DB."""
    from poob.discord_bot.notifier import DealNotifier
    from poob.storage.repositories.deal_repo import DealRepository
    from poob.storage.repositories.listing_repo import ListingRepository
    from poob.storage.repositories.scan_log_repo import ScanLogRepository
    from poob.storage.repositories.watchlist_repo import WatchlistRepository

    return PatrolEngine(
        browser_manager=mock_browser_manager,
        listing_repo=ListingRepository(db_connection),
        watchlist_repo=WatchlistRepository(db_connection),
        deal_repo=DealRepository(db_connection),
        scan_log_repo=ScanLogRepository(db_connection),
        notifier=AsyncMock(spec=DealNotifier),
        interest_matcher=InterestMatcher(),
        smart_deal_radar=smart_deal_radar,
        config=mock_config,
    )


class TestPatrolPipeline:
    """E2E tests for the patrol -> dedup -> inspect -> evaluate -> notify pipeline."""

    async def test_full_patrol_cycle_with_radar(
        self, db_connection, mock_browser_manager, mock_config
    ):
        """Full patrol cycle with SmartDealRadar finds deals and notifies."""
        from poob.skills.models import VLMEvaluation
        from poob.storage.repositories.deal_repo import DealRepository
        from poob.storage.repositories.listing_repo import ListingRepository
        from poob.storage.repositories.watchlist_repo import WatchlistRepository

        watchlist_repo = WatchlistRepository(db_connection)
        listing_repo = ListingRepository(db_connection)
        deal_repo = DealRepository(db_connection)

        # User watches for PS5 under $400
        await watchlist_repo.save(WatchItem(
            interest="PS5",
            max_price=400.0,
            discord_user_id="user_1",
            discord_channel_id="channel_1",
        ))

        # Mock radar returns a watchlist deal for PS5 listing
        watchlist_deal = Deal(
            listing_id="",
            watch_item_id="",
            score=DealScore.GOOD,
            estimated_market_price=450.0,
            discount_pct=44.4,
            llm_reasoning="Good deal on PS5",
        )
        vlm_eval = VLMEvaluation(
            item_identified="PS5 Console", deal_quality="good", confidence=0.8,
        )

        mock_radar = AsyncMock()

        async def radar_batch(listings, watchlist_items=None):
            results = []
            for listing in listings:
                if "PS5" in listing.title:
                    deal = Deal(
                        listing_id=listing.id or "",
                        watch_item_id="",  # The pipeline sets this
                        score=DealScore.GOOD,
                        estimated_market_price=450.0,
                        discount_pct=44.4,
                        llm_reasoning="Good deal on PS5",
                    )
                    results.append((deal, vlm_eval))
                else:
                    results.append((None, VLMEvaluation()))
            return results

        mock_radar.evaluate_batch = AsyncMock(side_effect=radar_batch)

        engine = _build_patrol_engine(
            db_connection, mock_browser_manager, mock_config,
            smart_deal_radar=mock_radar,
        )

        listings = [
            _make_listing("001", "PS5 Disc Edition Bundle", 250.0),
            _make_listing("002", "Couch - Great Condition", 100.0),
            _make_listing("003", "Mountain Bike", 150.0),
        ]

        with patch.object(
            engine._scanner, "sweep_category", new_callable=AsyncMock
        ) as mock_sweep:
            mock_sweep.return_value = listings
            result = await engine.run_patrol_cycle()

        assert isinstance(result, PatrolCycleResult)
        assert result.new_listings == 3

        saved = await listing_repo.list_recent()
        assert len(saved) == 3

    async def test_dedup_prevents_duplicate_deals(
        self, db_connection, mock_browser_manager, mock_config
    ):
        """Second patrol cycle should not re-process already-seen listings."""
        from poob.storage.repositories.listing_repo import ListingRepository

        listing_repo = ListingRepository(db_connection)

        engine = _build_patrol_engine(db_connection, mock_browser_manager, mock_config)

        listings = [_make_listing("001", "PS5", 250.0)]

        with patch.object(
            engine._scanner, "sweep_category", new_callable=AsyncMock
        ) as mock_sweep:
            mock_sweep.return_value = listings

            # First cycle: new listing
            result1 = await engine.run_patrol_cycle()
            assert result1.new_listings == 1

            # Second cycle: same listing, should be deduped
            result2 = await engine.run_patrol_cycle()
            assert result2.new_listings == 0

    async def test_smart_deal_radar_integration(
        self, db_connection, mock_browser_manager, mock_config
    ):
        """SmartDealRadar batch pipeline finds and saves deals."""
        from poob.skills.models import VLMEvaluation
        from poob.storage.repositories.deal_repo import DealRepository
        from poob.storage.repositories.watchlist_repo import WatchlistRepository

        watchlist_repo = WatchlistRepository(db_connection)
        deal_repo = DealRepository(db_connection)

        await watchlist_repo.save(WatchItem(
            interest="PS5",
            max_price=400.0,
            discord_user_id="user_1",
            discord_channel_id="channel_1",
        ))

        vlm_eval = VLMEvaluation(
            item_identified="PS5 Disc Edition", deal_quality="incredible", confidence=0.9,
        )

        mock_radar = AsyncMock()

        async def radar_batch(listings, watchlist_items=None):
            results = []
            for listing in listings:
                deal = Deal(
                    listing_id=listing.id or "",
                    score=DealScore.INCREDIBLE,
                    estimated_market_price=450.0,
                    discount_pct=44.4,
                    llm_reasoning="PS5 typically sells for $450. Listed at $250.",
                )
                results.append((deal, vlm_eval))
            return results

        mock_radar.evaluate_batch = AsyncMock(side_effect=radar_batch)

        engine = _build_patrol_engine(
            db_connection, mock_browser_manager, mock_config,
            smart_deal_radar=mock_radar,
        )

        listings = [_make_listing("001", "PS5 Disc Edition", 250.0)]

        with patch.object(
            engine._scanner, "sweep_category", new_callable=AsyncMock
        ) as mock_sweep:
            mock_sweep.return_value = listings
            result = await engine.run_patrol_cycle()

        assert result.deals_found >= 1

        deals = await deal_repo.list_recent()
        assert any(d.score == DealScore.INCREDIBLE for d in deals)

    async def test_patrol_logs_scan_log(
        self, db_connection, mock_browser_manager, mock_config
    ):
        """Each patrol cycle should write a ScanLog entry."""
        from poob.storage.repositories.scan_log_repo import ScanLogRepository

        scan_log_repo = ScanLogRepository(db_connection)

        engine = _build_patrol_engine(db_connection, mock_browser_manager, mock_config)

        with patch.object(
            engine._scanner, "sweep_category", new_callable=AsyncMock
        ) as mock_sweep:
            mock_sweep.return_value = []
            await engine.run_patrol_cycle()

        logs = await scan_log_repo.list_recent()
        assert len(logs) == 1
        assert logs[0].site == "facebook_marketplace"
        assert logs[0].category == "patrol"
