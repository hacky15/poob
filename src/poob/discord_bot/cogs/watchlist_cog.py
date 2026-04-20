"""Discord cog for watchlist commands: !watch, !unwatch, !watchlist."""

from __future__ import annotations

from typing import TYPE_CHECKING

from discord.ext import commands

from poob.discord_bot.formatter import format_watchlist_embed
from poob.storage.models import WatchItem
from poob.utils.logging import get_logger

if TYPE_CHECKING:
    from poob.storage.repositories.watchlist_repo import WatchlistRepository

log = get_logger("cogs.watchlist")


class WatchlistCog(commands.Cog, name="Watchlist"):
    """Commands for managing your interest list (things to look out for)."""

    def __init__(self, bot: commands.Bot, watchlist_repo: WatchlistRepository) -> None:
        self.bot = bot
        self._repo = watchlist_repo

    @commands.command(name="watch")
    async def watch(
        self,
        ctx: commands.Context,
        interest: str,
        max_price: float | None = None,
        notification_level: str = "good",
        location: str | None = None,
    ) -> None:
        """Add something to look out for on Marketplace.

        Usage:
          !watch "PS5" 300                    — PS5 under $300, good deals
          !watch "PS5" 300 great              — PS5 under $300, great+ deals only
          !watch "espresso machine" all       — any espresso machine listing
          !watch "furniture" incredible       — only incredible furniture deals
          !watch "free stuff" free            — only free items

        Notification levels: all, good, great, incredible, free
        Effort level: managed via natural language (DM me "max effort on TVs")
        """
        await self._do_watch(
            ctx,
            interest=interest,
            max_price=max_price,
            notification_level=notification_level,
            location=location,
        )

    async def _do_watch(
        self,
        ctx: commands.Context,
        *,
        interest: str,
        max_price: float | None,
        notification_level: str,
        location: str | None,
    ) -> None:
        """Internal implementation for the watch command."""
        valid_levels = {"all", "good", "great", "incredible", "free"}
        if notification_level not in valid_levels:
            notification_level = "good"

        item = WatchItem(
            interest=interest,
            max_price=max_price,
            notification_threshold=notification_level,
            location=location,
            discord_user_id=str(ctx.author.id),
            discord_channel_id=str(ctx.channel.id),
        )
        saved = await self._repo.save(item)
        price_str = f" (max ${max_price:.0f})" if max_price else ""
        level_str = f" [{notification_level}]" if notification_level != "good" else ""
        await ctx.send(
            f"Looking out for: **{interest}**{price_str}{level_str} (ID: `{saved.id}`)"
        )

    @commands.command(name="unwatch")
    async def unwatch(self, ctx: commands.Context, watch_id: str) -> None:
        """Remove an interest from your list.

        Usage: !unwatch <watch_id>
        """
        await self._do_unwatch(ctx, watch_id=watch_id)

    async def _do_unwatch(self, ctx: commands.Context, *, watch_id: str) -> None:
        """Internal implementation for the unwatch command."""
        deleted = await self._repo.delete(watch_id, str(ctx.author.id))
        if deleted:
            await ctx.send(f"Removed interest `{watch_id}`.")
        else:
            await ctx.send(f"Interest `{watch_id}` not found or not yours.")

    @commands.command(name="watchlist")
    async def watchlist(self, ctx: commands.Context) -> None:
        """Show your active interests."""
        await self._do_watchlist(ctx)

    async def _do_watchlist(self, ctx: commands.Context) -> None:
        """Internal implementation for the watchlist command."""
        items = await self._repo.list_for_user(str(ctx.author.id))
        embed = format_watchlist_embed(items)
        await ctx.send(embed=embed)
