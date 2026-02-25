"""Tests for repository CRUD operations with in-memory SQLite."""

from datetime import datetime, timezone

import pytest

from agentic_scraper.storage.models import Deal, DealScore, Listing, ScanLog, WatchItem
from agentic_scraper.storage.repositories.listing_repo import ListingRepository
from agentic_scraper.storage.repositories.watchlist_repo import WatchlistRepository
from agentic_scraper.storage.repositories.deal_repo import DealRepository
from agentic_scraper.storage.repositories.scan_log_repo import ScanLogRepository


class TestListingRepository:
    """CRUD operations for listings."""

    @pytest.fixture
    async def repo(self, db_connection):
        return ListingRepository(db_connection)

    async def test_save_and_get(self, repo, sample_listing):
        saved = await repo.save(sample_listing)
        assert saved.id is not None

        fetched = await repo.get(saved.id)
        assert fetched is not None
        assert fetched.title == "PlayStation 5 Disc Edition"
        assert fetched.price == 250.00
        assert fetched.site == "facebook_marketplace"

    async def test_exists_by_site_and_external_id(self, repo, sample_listing):
        assert await repo.exists("facebook_marketplace", "fb_12345") is False
        await repo.save(sample_listing)
        assert await repo.exists("facebook_marketplace", "fb_12345") is True

    async def test_exists_different_site(self, repo, sample_listing):
        await repo.save(sample_listing)
        assert await repo.exists("craigslist", "fb_12345") is False

    async def test_list_recent(self, repo):
        for i in range(5):
            listing = Listing(
                site="facebook_marketplace",
                external_id=f"fb_{i}",
                title=f"Item {i}",
                price=float(i * 10),
            )
            await repo.save(listing)

        recent = await repo.list_recent(limit=3)
        assert len(recent) == 3

    async def test_save_preserves_image_urls(self, repo, sample_listing):
        saved = await repo.save(sample_listing)
        fetched = await repo.get(saved.id)
        assert fetched.image_urls == ["https://example.com/ps5.jpg"]


class TestWatchlistRepository:
    """CRUD operations for watch items."""

    @pytest.fixture
    async def repo(self, db_connection):
        return WatchlistRepository(db_connection)

    async def test_save_and_get(self, repo, sample_watch_item):
        saved = await repo.save(sample_watch_item)
        assert saved.id is not None

        fetched = await repo.get(saved.id)
        assert fetched is not None
        assert fetched.keywords == "PS5"
        assert fetched.max_price == 300.00

    async def test_list_for_user(self, repo):
        item1 = WatchItem(keywords="PS5", discord_user_id="user_a")
        item2 = WatchItem(keywords="Xbox", discord_user_id="user_a")
        item3 = WatchItem(keywords="Switch", discord_user_id="user_b")

        await repo.save(item1)
        await repo.save(item2)
        await repo.save(item3)

        user_a_items = await repo.list_for_user("user_a")
        assert len(user_a_items) == 2
        keywords = {item.keywords for item in user_a_items}
        assert keywords == {"PS5", "Xbox"}

    async def test_list_active(self, repo):
        active = WatchItem(keywords="PS5", is_active=True)
        inactive = WatchItem(keywords="Xbox", is_active=False)
        await repo.save(active)
        await repo.save(inactive)

        active_items = await repo.list_active()
        assert len(active_items) == 1
        assert active_items[0].keywords == "PS5"

    async def test_delete(self, repo, sample_watch_item):
        saved = await repo.save(sample_watch_item)
        deleted = await repo.delete(saved.id, sample_watch_item.discord_user_id)
        assert deleted is True

        fetched = await repo.get(saved.id)
        assert fetched is None

    async def test_delete_wrong_user(self, repo, sample_watch_item):
        saved = await repo.save(sample_watch_item)
        deleted = await repo.delete(saved.id, "wrong_user")
        assert deleted is False

        fetched = await repo.get(saved.id)
        assert fetched is not None

    async def test_delete_nonexistent(self, repo):
        deleted = await repo.delete("nonexistent_id", "user_001")
        assert deleted is False


class TestDealRepository:
    """CRUD operations for deals."""

    @pytest.fixture
    async def repo(self, db_connection):
        return DealRepository(db_connection)

    @pytest.fixture
    async def listing_repo(self, db_connection):
        return ListingRepository(db_connection)

    async def test_save_and_get(self, repo, sample_deal):
        saved = await repo.save(sample_deal)
        assert saved.id is not None

        fetched = await repo.get(saved.id)
        assert fetched is not None
        assert fetched.score == DealScore.GREAT
        assert fetched.estimated_market_price == 400.00

    async def test_list_recent(self, repo):
        for i in range(5):
            deal = Deal(
                listing_id=f"listing_{i}",
                score=DealScore.GOOD,
            )
            await repo.save(deal)

        recent = await repo.list_recent(limit=3)
        assert len(recent) == 3

    async def test_list_unnotified(self, repo):
        notified = Deal(listing_id="a", notified=True)
        unnotified = Deal(listing_id="b", notified=False)
        await repo.save(notified)
        await repo.save(unnotified)

        result = await repo.list_unnotified()
        assert len(result) == 1
        assert result[0].listing_id == "b"

    async def test_mark_notified(self, repo):
        deal = Deal(listing_id="a", notified=False)
        saved = await repo.save(deal)

        await repo.mark_notified(saved.id)
        fetched = await repo.get(saved.id)
        assert fetched.notified is True
        assert fetched.notified_at is not None


class TestScanLogRepository:
    """CRUD operations for scan logs."""

    @pytest.fixture
    async def repo(self, db_connection):
        return ScanLogRepository(db_connection)

    async def test_save_and_get(self, repo, sample_scan_log):
        saved = await repo.save(sample_scan_log)
        assert saved.id is not None

        fetched = await repo.get(saved.id)
        assert fetched is not None
        assert fetched.site == "facebook_marketplace"
        assert fetched.listings_found == 15
        assert fetched.deals_found == 2

    async def test_list_recent(self, repo):
        for i in range(5):
            log = ScanLog(site="facebook_marketplace", query_keywords=f"query_{i}")
            await repo.save(log)

        recent = await repo.list_recent(limit=3)
        assert len(recent) == 3

    async def test_save_with_errors(self, repo):
        log = ScanLog(
            site="facebook_marketplace",
            query_keywords="test",
            errors=["timeout", "parse error"],
        )
        saved = await repo.save(log)
        fetched = await repo.get(saved.id)
        assert fetched.errors == ["timeout", "parse error"]
