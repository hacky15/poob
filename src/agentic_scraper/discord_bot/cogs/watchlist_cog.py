"""Discord cog for watchlist commands: !watch, !unwatch, !watchlist."""

from __future__ import annotations

from typing import TYPE_CHECKING

from discord.ext import commands

from agentic_scraper.discord_bot.formatter import format_watchlist_embed
from agentic_scraper.storage.models import WatchItem
from agentic_scraper.utils.logging import get_logger

if TYPE_CHECKING:
    from agentic_scraper.storage.repositories.watchlist_repo import WatchlistRepository

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
        location: str | None = None,
    ) -> None:
        """Add something to look out for on Marketplace.

        Usage: !watch "PS5" 300 "Appleton, WI"
        """
        await self._do_watch(ctx, interest=interest, max_price=max_price, location=location)

    async def _do_watch(
        self,
        ctx: commands.Context,
        *,
        interest: str,
        max_price: float | None,
        location: str | None,
    ) -> None:
        """Internal implementation for the watch command."""
        item = WatchItem(
            interest=interest,
            max_price=max_price,
            location=location,
            discord_user_id=str(ctx.author.id),
            discord_channel_id=str(ctx.channel.id),
        )
        saved = await self._repo.save(item)
        price_str = f" (max ${max_price:.0f})" if max_price else ""
        await ctx.send(f"Looking out for: **{interest}**{price_str} (ID: `{saved.id}`)")

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
