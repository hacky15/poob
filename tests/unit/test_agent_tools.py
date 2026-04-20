"""Tests for agent tool definitions."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from poob.storage.models import Deal, DealScore, Listing, WatchItem
from poob.storage.repositories.preferences_repo import UserPreferencesRepository
from poob.storage.repositories.watchlist_repo import WatchlistRepository


@pytest.fixture
async def prefs_repo(db_connection):
    return UserPreferencesRepository(db_connection)


@pytest.fixture
async def watchlist_repo(db_connection):
    return WatchlistRepository(db_connection)


@pytest.fixture
def deal_repo():
    repo = AsyncMock()
    repo.list_recent = AsyncMock(return_value=[])
    return repo


@pytest.fixture
def scheduler():
    s = MagicMock()
    s.trigger_now = AsyncMock()
    s.pause = MagicMock()
    s.resume = MagicMock()
    s.is_running = True
    s.is_paused = False
    s.last_scan_time = None
    s.next_scan_time = None
    return s


@pytest.fixture
def listing_repo():
    return AsyncMock()


@pytest.fixture
def scan_log_repo():
    repo = AsyncMock()
    repo.get_stats = AsyncMock(return_value={
        "total_scans": 0, "total_listings": 0, "total_deals": 0,
        "total_errors": 0, "avg_duration": 0.0,
    })
    repo.list_recent = AsyncMock(return_value=[])
    return repo


def _build_tools(
    user_id, channel_id, prefs_repo, watchlist_repo, deal_repo, listing_repo,
    scheduler, scan_log_repo=None,
):
    from poob.agent.tools import build_tools

    return build_tools(
        discord_user_id=user_id,
        discord_channel_id=channel_id,
        prefs_repo=prefs_repo,
        watchlist_repo=watchlist_repo,
        deal_repo=deal_repo,
        listing_repo=listing_repo,
        scheduler=scheduler,
        scan_log_repo=scan_log_repo,
    )


class TestAddToWishlist:
    async def test_creates_watch_item(
        self, prefs_repo, watchlist_repo, deal_repo, listing_repo, scheduler, db_connection
    ):
        tools = _build_tools(
            "user_1", "chan_1", prefs_repo, watchlist_repo, deal_repo, listing_repo, scheduler
        )
        add_tool = next(t for t in tools if t.name == "add_to_wishlist")

        result = await add_tool.ainvoke({"item_name": "espresso machine", "max_price": 200})

        # Verify watch item created in DB
        items = await watchlist_repo.list_for_user("user_1")
        assert len(items) == 1
        assert items[0].interest == "espresso machine"
        assert items[0].max_price == 200.0

    async def test_updates_preferences(
        self, prefs_repo, watchlist_repo, deal_repo, listing_repo, scheduler, db_connection
    ):
        tools = _build_tools(
            "user_1", "chan_1", prefs_repo, watchlist_repo, deal_repo, listing_repo, scheduler
        )
        add_tool = next(t for t in tools if t.name == "add_to_wishlist")

        await add_tool.ainvoke({"item_name": "rug", "priority": "high"})

        wishlist_json = await prefs_repo.get("user_1", "wishlist")
        wishlist = json.loads(wishlist_json)
        assert len(wishlist) == 1
        assert wishlist[0]["name"] == "rug"
        assert wishlist[0]["priority"] == "high"

    async def test_add_multiple_items(
        self, prefs_repo, watchlist_repo, deal_repo, listing_repo, scheduler, db_connection
    ):
        tools = _build_tools(
            "user_1", "chan_1", prefs_repo, watchlist_repo, deal_repo, listing_repo, scheduler
        )
        add_tool = next(t for t in tools if t.name == "add_to_wishlist")

        await add_tool.ainvoke({"item_name": "rug"})
        await add_tool.ainvoke({"item_name": "chair", "max_price": 50})

        wishlist_json = await prefs_repo.get("user_1", "wishlist")
        wishlist = json.loads(wishlist_json)
        assert len(wishlist) == 2


class TestRemoveFromWishlist:
    async def test_deactivates_watch_item(
        self, prefs_repo, watchlist_repo, deal_repo, listing_repo, scheduler, db_connection
    ):
        tools = _build_tools(
            "user_1", "chan_1", prefs_repo, watchlist_repo, deal_repo, listing_repo, scheduler
        )
        add_tool = next(t for t in tools if t.name == "add_to_wishlist")
        remove_tool = next(t for t in tools if t.name == "remove_from_wishlist")

        await add_tool.ainvoke({"item_name": "espresso machine"})
        result = await remove_tool.ainvoke({"item_name": "espresso machine"})

        assert "removed" in result.lower() or "Removed" in result

        # Watch item should be deactivated
        items = await watchlist_repo.list_active()
        assert len(items) == 0

    async def test_remove_nonexistent_returns_message(
        self, prefs_repo, watchlist_repo, deal_repo, listing_repo, scheduler, db_connection
    ):
        tools = _build_tools(
            "user_1", "chan_1", prefs_repo, watchlist_repo, deal_repo, listing_repo, scheduler
        )
        remove_tool = next(t for t in tools if t.name == "remove_from_wishlist")

        result = await remove_tool.ainvoke({"item_name": "nonexistent"})
        assert "not found" in result.lower() or "no item" in result.lower()


class TestShowWishlist:
    async def test_empty_wishlist(
        self, prefs_repo, watchlist_repo, deal_repo, listing_repo, scheduler, db_connection
    ):
        tools = _build_tools(
            "user_1", "chan_1", prefs_repo, watchlist_repo, deal_repo, listing_repo, scheduler
        )
        show_tool = next(t for t in tools if t.name == "show_wishlist")

        result = await show_tool.ainvoke({})
        assert "empty" in result.lower() or "no items" in result.lower()

    async def test_shows_items(
        self, prefs_repo, watchlist_repo, deal_repo, listing_repo, scheduler, db_connection
    ):
        tools = _build_tools(
            "user_1", "chan_1", prefs_repo, watchlist_repo, deal_repo, listing_repo, scheduler
        )
        add_tool = next(t for t in tools if t.name == "add_to_wishlist")
        show_tool = next(t for t in tools if t.name == "show_wishlist")

        await add_tool.ainvoke({"item_name": "rug", "max_price": 100})
        result = await show_tool.ainvoke({})
        assert "rug" in result.lower()


class TestTriggerScan:
    async def test_calls_scheduler(
        self, prefs_repo, watchlist_repo, deal_repo, listing_repo, scheduler, db_connection
    ):
        tools = _build_tools(
            "user_1", "chan_1", prefs_repo, watchlist_repo, deal_repo, listing_repo, scheduler
        )
        scan_tool = next(t for t in tools if t.name == "trigger_scan")

        result = await scan_tool.ainvoke({})
        scheduler.trigger_now.assert_called_once()
        assert "scan" in result.lower() or "patrol" in result.lower()


class TestGetRecentDeals:
    async def test_no_deals(
        self, prefs_repo, watchlist_repo, deal_repo, listing_repo, scheduler, db_connection
    ):
        tools = _build_tools(
            "user_1", "chan_1", prefs_repo, watchlist_repo, deal_repo, listing_repo, scheduler
        )
        deals_tool = next(t for t in tools if t.name == "get_recent_deals")

        result = await deals_tool.ainvoke({})
        assert "no deals" in result.lower() or "no recent" in result.lower()

    async def test_returns_deals_with_url_and_reasoning(
        self, prefs_repo, watchlist_repo, listing_repo, scheduler, db_connection
    ):
        mock_deal_repo = AsyncMock()
        mock_deal = Deal(
            listing_id="lst_1",
            score=DealScore.GREAT,
            estimated_market_price=400.0,
            discount_pct=37.5,
            llm_reasoning="Great deal on PS5",
        )
        mock_deal_repo.list_recent = AsyncMock(return_value=[mock_deal])

        mock_listing = Listing(
            id="lst_1",
            site="facebook_marketplace",
            external_id="fb_1",
            title="PS5 Disc Edition",
            price=250.0,
            location="Appleton, WI",
            listing_url="https://facebook.com/marketplace/item/123",
        )
        listing_repo_mock = AsyncMock()
        listing_repo_mock.get = AsyncMock(return_value=mock_listing)

        tools = _build_tools(
            "user_1", "chan_1", prefs_repo, watchlist_repo,
            mock_deal_repo, listing_repo_mock, scheduler,
        )
        deals_tool = next(t for t in tools if t.name == "get_recent_deals")

        result = await deals_tool.ainvoke({"count": 5})
        assert "PS5" in result
        assert "Appleton" in result
        assert "facebook.com" in result
        assert "Great deal" in result


class TestGetScannerStatus:
    async def test_returns_status(
        self, prefs_repo, watchlist_repo, deal_repo, listing_repo, scheduler, db_connection
    ):
        tools = _build_tools(
            "user_1", "chan_1", prefs_repo, watchlist_repo, deal_repo, listing_repo, scheduler
        )
        status_tool = next(t for t in tools if t.name == "get_scanner_status")

        result = await status_tool.ainvoke({})
        assert "running" in result.lower() or "status" in result.lower()


class TestUpdatePreferences:
    async def test_updates_location(
        self, prefs_repo, watchlist_repo, deal_repo, listing_repo, scheduler, db_connection
    ):
        tools = _build_tools(
            "user_1", "chan_1", prefs_repo, watchlist_repo, deal_repo, listing_repo, scheduler
        )
        prefs_tool = next(t for t in tools if t.name == "update_preferences")

        await prefs_tool.ainvoke({"location": "Portland, OR", "radius_miles": 25})

        loc_json = await prefs_repo.get("user_1", "location")
        loc = json.loads(loc_json)
        assert loc["city"] == "Portland, OR"
        assert loc["radius_miles"] == 25

    async def test_updates_search_priorities(
        self, prefs_repo, watchlist_repo, deal_repo, listing_repo, scheduler, db_connection
    ):
        tools = _build_tools(
            "user_1", "chan_1", prefs_repo, watchlist_repo, deal_repo, listing_repo, scheduler
        )
        prefs_tool = next(t for t in tools if t.name == "update_preferences")

        await prefs_tool.ainvoke({"just_listed_first": True, "desperate_seller_detection": True})

        prio_json = await prefs_repo.get("user_1", "search_priorities")
        prio = json.loads(prio_json)
        assert prio["just_listed_first"] is True
        assert prio["desperate_seller_detection"] is True


class TestPausePatrol:
    async def test_pauses_scheduler(
        self, prefs_repo, watchlist_repo, deal_repo, listing_repo, scheduler, db_connection
    ):
        tools = _build_tools(
            "user_1", "chan_1", prefs_repo, watchlist_repo, deal_repo, listing_repo, scheduler
        )
        pause_tool = next(t for t in tools if t.name == "pause_patrol")

        result = await pause_tool.ainvoke({})
        scheduler.pause.assert_called_once()
        assert "paused" in result.lower()


class TestResumePatrol:
    async def test_resumes_scheduler(
        self, prefs_repo, watchlist_repo, deal_repo, listing_repo, scheduler, db_connection
    ):
        tools = _build_tools(
            "user_1", "chan_1", prefs_repo, watchlist_repo, deal_repo, listing_repo, scheduler
        )
        resume_tool = next(t for t in tools if t.name == "resume_patrol")

        result = await resume_tool.ainvoke({})
        scheduler.resume.assert_called_once()
        assert "resumed" in result.lower()


class TestSearchListings:
    async def test_search_by_keyword(
        self, prefs_repo, watchlist_repo, deal_repo, scheduler, db_connection
    ):
        mock_listing = Listing(
            id="l1", site="facebook_marketplace", external_id="fb_1",
            title="PS5 Disc Edition", price=250.0, location="Appleton, WI",
            listing_url="https://facebook.com/marketplace/item/111",
        )
        listing_repo_mock = AsyncMock()
        listing_repo_mock.search = AsyncMock(return_value=[mock_listing])

        tools = _build_tools(
            "user_1", "chan_1", prefs_repo, watchlist_repo,
            deal_repo, listing_repo_mock, scheduler,
        )
        search_tool = next(t for t in tools if t.name == "search_listings")

        result = await search_tool.ainvoke({"keyword": "PS5"})
        assert "PS5 Disc Edition" in result
        assert "Appleton" in result

    async def test_search_by_price_range(
        self, prefs_repo, watchlist_repo, deal_repo, scheduler, db_connection
    ):
        listing_repo_mock = AsyncMock()
        listing_repo_mock.search = AsyncMock(return_value=[])

        tools = _build_tools(
            "user_1", "chan_1", prefs_repo, watchlist_repo,
            deal_repo, listing_repo_mock, scheduler,
        )
        search_tool = next(t for t in tools if t.name == "search_listings")

        result = await search_tool.ainvoke({"max_price": 50.0})
        assert "no listings" in result.lower()


class TestGetDealDetails:
    async def test_returns_full_details(
        self, prefs_repo, watchlist_repo, scheduler, db_connection
    ):
        mock_deal = Deal(
            listing_id="lst_1",
            score=DealScore.GREAT,
            estimated_market_price=400.0,
            discount_pct=37.5,
            llm_reasoning="eBay sold median $400. This is 37.5% below market.",
        )
        mock_deal_repo = AsyncMock()
        mock_deal_repo.list_recent = AsyncMock(return_value=[mock_deal])

        mock_listing = Listing(
            id="lst_1", site="facebook_marketplace", external_id="fb_1",
            title="PS5 Disc Edition", price=250.0, location="Appleton, WI",
            description="Like new, barely used",
            listing_url="https://facebook.com/marketplace/item/123",
            image_urls=["https://example.com/ps5.jpg"],
        )
        listing_repo_mock = AsyncMock()
        listing_repo_mock.get = AsyncMock(return_value=mock_listing)

        tools = _build_tools(
            "user_1", "chan_1", prefs_repo, watchlist_repo,
            mock_deal_repo, listing_repo_mock, scheduler,
        )
        detail_tool = next(t for t in tools if t.name == "get_deal_details")

        result = await detail_tool.ainvoke({"deal_number": 1})
        assert "PS5 Disc Edition" in result
        assert "$400.00" in result
        assert "38%" in result or "37" in result
        assert "eBay sold" in result
        assert "1 photo" in result

    async def test_deal_not_found(
        self, prefs_repo, watchlist_repo, deal_repo, listing_repo, scheduler, db_connection
    ):
        tools = _build_tools(
            "user_1", "chan_1", prefs_repo, watchlist_repo, deal_repo, listing_repo, scheduler
        )
        detail_tool = next(t for t in tools if t.name == "get_deal_details")

        result = await detail_tool.ainvoke({"deal_number": 1})
        assert "not found" in result.lower()


class TestGetPreferences:
    async def test_shows_preferences(
        self, prefs_repo, watchlist_repo, deal_repo, listing_repo, scheduler, db_connection
    ):
        await prefs_repo.set("user_1", "location", json.dumps({"city": "Appleton, WI", "radius_miles": 20}))
        await prefs_repo.set("user_1", "wishlist", json.dumps([{"name": "PS5"}]))

        tools = _build_tools(
            "user_1", "chan_1", prefs_repo, watchlist_repo, deal_repo, listing_repo, scheduler
        )
        prefs_tool = next(t for t in tools if t.name == "get_preferences")

        result = await prefs_tool.ainvoke({})
        assert "Appleton" in result
        assert "20 mile" in result
        assert "1 item" in result

    async def test_no_preferences(
        self, prefs_repo, watchlist_repo, deal_repo, listing_repo, scheduler, db_connection
    ):
        tools = _build_tools(
            "user_1", "chan_1", prefs_repo, watchlist_repo, deal_repo, listing_repo, scheduler
        )
        prefs_tool = next(t for t in tools if t.name == "get_preferences")

        result = await prefs_tool.ainvoke({})
        assert "no preferences" in result.lower()


class TestGetScanHistory:
    async def test_shows_stats(
        self, prefs_repo, watchlist_repo, deal_repo, listing_repo, scheduler,
        scan_log_repo, db_connection,
    ):
        scan_log_repo.get_stats = AsyncMock(return_value={
            "total_scans": 10, "total_listings": 50, "total_deals": 3,
            "total_errors": 1, "avg_duration": 25.0,
        })

        tools = _build_tools(
            "user_1", "chan_1", prefs_repo, watchlist_repo, deal_repo,
            listing_repo, scheduler, scan_log_repo,
        )
        history_tool = next(t for t in tools if t.name == "get_scan_history")

        result = await history_tool.ainvoke({"hours": 24})
        assert "10" in result  # total_scans
        assert "50" in result  # total_listings
        assert "3" in result  # total_deals

    async def test_no_scan_log_repo(
        self, prefs_repo, watchlist_repo, deal_repo, listing_repo, scheduler, db_connection
    ):
        """When scan_log_repo is None, should return unavailable message."""
        tools = _build_tools(
            "user_1", "chan_1", prefs_repo, watchlist_repo, deal_repo,
            listing_repo, scheduler, None,
        )
        history_tool = next(t for t in tools if t.name == "get_scan_history")

        result = await history_tool.ainvoke({})
        assert "not available" in result.lower()
