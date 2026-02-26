"""ScraperBot - main Discord bot class."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord.ext import commands

from agentic_scraper.utils.logging import get_logger

if TYPE_CHECKING:
    from agentic_scraper.config import AppConfig
    from agentic_scraper.discord_bot.notifier import DealNotifier
    from agentic_scraper.scanner.engine import ScanEngine
    from agentic_scraper.scanner.scheduler import ScanScheduler
    from agentic_scraper.sites.registry import SiteRegistry
    from agentic_scraper.storage.repositories.watchlist_repo import WatchlistRepository

log = get_logger("discord.bot")


class ScraperBot(commands.Bot):
    """Discord bot for the Agentic Web Scraper.

    Manages cog loading and holds references to core components
    so cogs can access them.

    Args:
        config: Application configuration.
    """

    def __init__(self, config: AppConfig) -> None:
        intents = discord.Intents.default()
        intents.message_content = True

        super().__init__(
            command_prefix=config.discord_command_prefix,
            intents=intents,
        )

        self.config = config
        self.scan_engine: ScanEngine | None = None
        self.scan_scheduler: ScanScheduler | None = None
        self.notifier: DealNotifier | None = None
        self.site_registry: SiteRegistry | None = None
        self.watchlist_repo: WatchlistRepository | None = None

    async def setup_hook(self) -> None:
        """Load all cog extensions when the bot starts."""
        from agentic_scraper.discord_bot.cogs.admin_cog import AdminCog
        from agentic_scraper.discord_bot.cogs.scanning_cog import ScanningCog
        from agentic_scraper.discord_bot.cogs.search_cog import SearchCog
        from agentic_scraper.discord_bot.cogs.watchlist_cog import WatchlistCog

        if self.watchlist_repo:
            await self.add_cog(WatchlistCog(bot=self, watchlist_repo=self.watchlist_repo))

        if self.scan_scheduler and self.site_registry:
            await self.add_cog(ScanningCog(
                bot=self,
                scheduler=self.scan_scheduler,
                registry=self.site_registry,
            ))

        if self.site_registry:
            await self.add_cog(AdminCog(bot=self, registry=self.site_registry))

        await self.add_cog(SearchCog(bot=self))

        log.info("All cogs loaded")

    async def on_ready(self) -> None:
        """Called when the bot has connected to Discord."""
        log.info("Bot is ready", user=str(self.user), guilds=len(self.guilds))
