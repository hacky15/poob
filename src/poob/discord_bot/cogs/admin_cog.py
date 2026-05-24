"""Discord cog for admin commands: !config, !sites, !logs."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord.ext import commands

from poob.utils.logging import get_logger

if TYPE_CHECKING:
    from poob.sites.registry import SiteRegistry
    from poob.storage.repositories.scan_log_repo import ScanLogRepository

log = get_logger("cogs.admin")


class AdminCog(commands.Cog, name="Admin"):
    """Administrative commands for bot management."""

    def __init__(
        self,
        bot: commands.Bot,
        registry: SiteRegistry,
        scan_log_repo: ScanLogRepository | None = None,
    ) -> None:
        self.bot = bot
        self._registry = registry
        self._scan_log_repo = scan_log_repo

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
        """Show the most recent scan logs.

        Usage: ``!logs`` (default 5) or ``!logs 10`` to override the count.
        Clamped to 1..50 to keep the embed sane.
        """
        if self._scan_log_repo is None:
            await ctx.send("Scan-log storage isn't wired on this stack.")
            return
        count = max(1, min(count, 50))

        try:
            logs = await self._scan_log_repo.list_recent(limit=count)
        except Exception as exc:
            log.warning("admin.logs query failed", error=str(exc)[:120])
            await ctx.send(f"Couldn't fetch scan logs: {exc}")
            return

        if not logs:
            await ctx.send("No scan logs recorded yet.")
            return

        embed = discord.Embed(
            title=f"Last {len(logs)} scan log{'s' if len(logs) != 1 else ''}",
            color=discord.Colour.blue(),
        )
        for entry in logs:
            stamp = entry.started_at.strftime("%Y-%m-%d %H:%M UTC")
            err_count = len(entry.errors) if entry.errors else 0
            value = (
                f"site: `{entry.site}` · category: `{entry.category}`\n"
                f"listings: **{entry.listings_found}** · deals: **{entry.deals_found}** · "
                f"errors: **{err_count}** · duration: **{entry.duration_seconds:.1f}s**"
            )
            embed.add_field(name=stamp, value=value, inline=False)

        await ctx.send(embed=embed)
