"""Discord cog for deal history: !deals."""

from __future__ import annotations

from typing import TYPE_CHECKING

from discord.ext import commands

from agentic_scraper.discord_bot.formatter import format_deal_embed
from agentic_scraper.utils.logging import get_logger

if TYPE_CHECKING:
    from agentic_scraper.storage.repositories.deal_repo import DealRepository
    from agentic_scraper.storage.repositories.listing_repo import ListingRepository

log = get_logger("cogs.search")


class SearchCog(commands.Cog, name="Deals"):
    """Commands for viewing deal history."""

    def __init__(
        self,
        bot: commands.Bot,
        deal_repo: DealRepository | None = None,
        listing_repo: ListingRepository | None = None,
    ) -> None:
        self.bot = bot
        self._deal_repo = deal_repo
        self._listing_repo = listing_repo

    @commands.command(name="deals")
    async def deals(self, ctx: commands.Context, count: int = 5) -> None:
        """Show recent deals found by the patrol system.

        Usage: !deals 10
        """
        if not self._deal_repo or not self._listing_repo:
            await ctx.send("Deal history not available yet.")
            return

        deals = await self._deal_repo.list_recent(count)
        if not deals:
            await ctx.send("No recent deals found.")
            return

        for deal in deals:
            listing = await self._listing_repo.get(deal.listing_id)
            if listing:
                embed = format_deal_embed(deal, listing)
                await ctx.send(embed=embed)
