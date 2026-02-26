"""Discord cog for search commands: !search, !deals."""

from __future__ import annotations

from discord.ext import commands

from agentic_scraper.utils.logging import get_logger

log = get_logger("cogs.search")


class SearchCog(commands.Cog, name="Search"):
    """Commands for one-shot searches and deal history."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.command(name="search")
    async def search(self, ctx: commands.Context, *, keywords: str) -> None:
        """One-shot search, returns top results immediately.

        Usage: !search PS5
        """
        # Phase 3 stub - will be fully implemented when scan engine is wired
        await ctx.send(f"Searching for **{keywords}**... (coming in Phase 4)")

    @commands.command(name="deals")
    async def deals(self, ctx: commands.Context, count: int = 5) -> None:
        """Show recent deals from history.

        Usage: !deals 10
        """
        # Phase 3 stub - will be fully implemented when deal repo is wired
        await ctx.send(f"Fetching last {count} deals... (coming in Phase 4)")
