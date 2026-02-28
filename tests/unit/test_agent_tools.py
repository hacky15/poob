"""Tests for agent tool definitions."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from agentic_scraper.storage.models import Deal, DealScore, Listing, WatchItem
from agentic_scraper.storage.repositories.preferences_repo import UserPreferencesRepository
from agentic_scraper.storage.repositories.watchlist_repo import WatchlistRepository


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
    s = AsyncMock()
    s.trigger_now = AsyncMock()
    s.is_running = True
    s.is_paused = False
    s.last_scan_time = None
    s.next_scan_time = None
    return s


@pytest.fixture
def listing_repo():
    return AsyncMock()


def _build_tools(
    user_id, channel_id, prefs_repo, watchlist_repo, deal_repo, listing_repo, scheduler
):
    from agentic_scraper.agent.tools import build_tools

    return build_tools(
        discord_user_id=user_id,
        discord_channel_id=channel_id,
        prefs_repo=prefs_repo,
        watchlist_repo=watchlist_repo,
        deal_repo=deal_repo,
        listing_repo=listing_repo,
        scheduler=scheduler,
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
        assert items[0].keywords == "espresso machine"
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
        assert "scan" in result.lower()


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

    async def test_returns_deals(
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
        )
        listing_repo_mock = AsyncMock()
        listing_repo_mock.get = AsyncMock(return_value=mock_listing)

        tools = _build_tools(
            "user_1", "chan_1", prefs_repo, watchlist_repo,
            mock_deal_repo, listing_repo_mock, scheduler,
        )
        deals_tool = next(t for t in tools if t.name == "get_recent_deals")

        result = await deals_tool.ainvoke({"count": 5})
        assert "PS5" in result or "ps5" in result.lower()


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
