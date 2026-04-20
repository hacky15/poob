"""Integration tests for Discord bot commands."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from poob.storage.models import WatchItem


class TestWatchlistCog:
    """Tests for watchlist commands (!watch, !unwatch, !watchlist)."""

    async def test_watch_command_creates_item(self, db_connection):
        """!watch should save a WatchItem to the database."""
        from poob.discord_bot.cogs.watchlist_cog import WatchlistCog
        from poob.storage.repositories.watchlist_repo import WatchlistRepository

        repo = WatchlistRepository(db_connection)
        cog = WatchlistCog(bot=MagicMock(), watchlist_repo=repo)

        # Mock the Discord context
        ctx = AsyncMock()
        ctx.author.id = "user_123"
        ctx.channel.id = "channel_456"
        ctx.send = AsyncMock()

        await cog._do_watch(
            ctx, interest="PS5", max_price=300.0, location=None, notification_level="good",
        )

        items = await repo.list_for_user("user_123")
        assert len(items) == 1
        assert items[0].interest == "PS5"
        assert items[0].max_price == 300.0
        ctx.send.assert_called_once()

    async def test_unwatch_command_removes_item(self, db_connection):
        """!unwatch should delete the specified watch item."""
        from poob.discord_bot.cogs.watchlist_cog import WatchlistCog
        from poob.storage.repositories.watchlist_repo import WatchlistRepository

        repo = WatchlistRepository(db_connection)
        watch = await repo.save(WatchItem(
            interest="PS5", max_price=300.0,
            discord_user_id="user_123", discord_channel_id="channel_456",
        ))

        cog = WatchlistCog(bot=MagicMock(), watchlist_repo=repo)

        ctx = AsyncMock()
        ctx.author.id = "user_123"
        ctx.send = AsyncMock()

        await cog._do_unwatch(ctx, watch_id=watch.id)

        items = await repo.list_for_user("user_123")
        assert len(items) == 0
        ctx.send.assert_called_once()

    async def test_watchlist_command_returns_items(self, db_connection):
        """!watchlist should send an embed with user's watches."""
        from poob.discord_bot.cogs.watchlist_cog import WatchlistCog
        from poob.storage.repositories.watchlist_repo import WatchlistRepository

        repo = WatchlistRepository(db_connection)
        await repo.save(WatchItem(
            interest="PS5", max_price=300.0,
            discord_user_id="user_123", discord_channel_id="c1",
        ))
        await repo.save(WatchItem(
            interest="Xbox", max_price=400.0,
            discord_user_id="user_123", discord_channel_id="c1",
        ))

        cog = WatchlistCog(bot=MagicMock(), watchlist_repo=repo)

        ctx = AsyncMock()
        ctx.author.id = "user_123"
        ctx.send = AsyncMock()

        await cog._do_watchlist(ctx)

        ctx.send.assert_called_once()
        # Should send an embed
        call_kwargs = ctx.send.call_args
        assert call_kwargs is not None


class TestScanningCog:
    """Tests for scanning commands (!scan, !status)."""

    async def test_status_command_returns_embed(self):
        """!status should return a status embed."""
        from poob.discord_bot.cogs.scanning_cog import ScanningCog

        mock_scheduler = MagicMock()
        mock_scheduler.is_running = True
        mock_scheduler.is_paused = False
        mock_scheduler.last_scan_time = None
        mock_scheduler.next_scan_time = None

        mock_registry = MagicMock()
        mock_registry.list_sites.return_value = ["facebook_marketplace"]

        cog = ScanningCog(
            bot=MagicMock(),
            scheduler=mock_scheduler,
            registry=mock_registry,
        )

        ctx = AsyncMock()
        ctx.send = AsyncMock()

        await cog._do_status(ctx)
        ctx.send.assert_called_once()

    async def test_scan_triggers_immediate_cycle(self):
        """!scan should trigger an immediate scan."""
        from poob.discord_bot.cogs.scanning_cog import ScanningCog

        mock_scheduler = MagicMock()
        mock_scheduler.trigger_now = AsyncMock()
        mock_scheduler.is_running = True
        mock_scheduler.is_paused = False
        mock_scheduler.last_scan_time = None
        mock_scheduler.next_scan_time = None

        cog = ScanningCog(
            bot=MagicMock(),
            scheduler=mock_scheduler,
            registry=MagicMock(),
        )

        ctx = AsyncMock()
        ctx.send = AsyncMock()

        await cog._do_scan(ctx)
        mock_scheduler.trigger_now.assert_called_once()


class TestAdminCog:
    """Tests for admin commands (!sites)."""

    async def test_sites_lists_registered_adapters(self):
        """!sites should list all registered site adapters."""
        from poob.discord_bot.cogs.admin_cog import AdminCog

        mock_registry = MagicMock()
        mock_registry.list_sites.return_value = ["facebook_marketplace"]

        adapter = MagicMock()
        adapter.site_name = "facebook_marketplace"
        adapter.base_url = "https://facebook.com/marketplace"
        adapter.requires_login = True
        mock_registry.get.return_value = adapter

        cog = AdminCog(bot=MagicMock(), registry=mock_registry)

        ctx = AsyncMock()
        ctx.send = AsyncMock()

        await cog._do_sites(ctx)
        ctx.send.assert_called_once()
