"""Discord cog for admin commands: !config, !sites, !logs."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord.ext import commands

from agentic_scraper.utils.logging import get_logger

if TYPE_CHECKING:
    from agentic_scraper.sites.registry import SiteRegistry

log = get_logger("cogs.admin")


class AdminCog(commands.Cog, name="Admin"):
    """Administrative commands for bot management."""

    def __init__(self, bot: commands.Bot, registry: SiteRegistry) -> None:
        self.bot = bot
        self._registry = registry

    @commands.command(name="sites")
    async def sites(self, ctx: commands.Context) -> None:
        """List all registered site adapters."""
        await self._do_sites(ctx)

    async def _do_sites(self, ctx: commands.Context) -> None:
        """Internal implementation for the sites command."""
        site_names = self._registry.list_sites()

        if not site_names:
            await ctx.send("No site adapters registered.")
            return

        embed = discord.Embed(title="Registered Sites", color=discord.Colour.blue())

        for name in site_names:
            adapter = self._registry.get(name)
            if adapter:
                login_str = "Yes" if adapter.requires_login else "No"
                embed.add_field(
                    name=name,
                    value=f"URL: {adapter.base_url}\nRequires login: {login_str}",
                    inline=False,
                )

        await ctx.send(embed=embed)

    @commands.command(name="logs")
    async def logs(self, ctx: commands.Context, count: int = 5) -> None:
        """Show recent scan logs.

        Usage: !logs 10
        """
        # Stub - will be wired to scan_log_repo in Phase 4
        await ctx.send(f"Fetching last {count} scan logs... (coming in Phase 4)")
