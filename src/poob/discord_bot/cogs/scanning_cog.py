"""Discord cog for patrol commands: !scan, !pause, !resume, !status."""

from __future__ import annotations

from typing import TYPE_CHECKING

from discord.ext import commands

from poob.discord_bot.formatter import format_status_embed
from poob.utils.logging import get_logger

if TYPE_CHECKING:
    from poob.sites.registry import SiteRegistry

log = get_logger("cogs.scanning")


class ScanningCog(commands.Cog, name="Scanning"):
    """Commands for controlling the patrol scheduler."""

    def __init__(
        self,
        bot: commands.Bot,
        scheduler: object,
        registry: SiteRegistry,
    ) -> None:
        self.bot = bot
        self._scheduler = scheduler
        self._registry = registry

    @commands.command(name="scan")
    async def scan(self, ctx: commands.Context) -> None:
        """Trigger an immediate patrol cycle."""
        await self._do_scan(ctx)

    async def _do_scan(self, ctx: commands.Context) -> None:
        """Internal implementation for the scan command."""
        await ctx.send("Triggering immediate patrol...")
        await self._scheduler.trigger_now()

    @commands.command(name="pause")
    async def pause(self, ctx: commands.Context) -> None:
        """Pause the automatic patrol scheduler."""
        self._scheduler.pause()
        await ctx.send("Patrol paused. Use `!resume` to continue.")

    @commands.command(name="resume")
    async def resume(self, ctx: commands.Context) -> None:
        """Resume the automatic patrol scheduler."""
        self._scheduler.resume()
        await ctx.send("Patrol resumed.")

    @commands.command(name="status")
    async def status(self, ctx: commands.Context) -> None:
        """Show scanner status."""
        await self._do_status(ctx)

    async def _do_status(self, ctx: commands.Context) -> None:
        """Internal implementation for the status command."""
        embed = format_status_embed(
            is_running=self._scheduler.is_running,
            is_paused=self._scheduler.is_paused,
            last_scan_time=self._scheduler.last_scan_time,
            next_scan_time=self._scheduler.next_scan_time,
            registered_sites=self._registry.list_sites(),
        )
        await ctx.send(embed=embed)
