"""ScraperBot - main Discord bot class."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord.ext import commands

from agentic_scraper.utils.logging import get_logger

if TYPE_CHECKING:
    from agentic_scraper.brain.poob import PoobBrain
    from agentic_scraper.config import AppConfig
    from agentic_scraper.discord_bot.notifier import DealNotifier
    from agentic_scraper.scanner.patrol_engine import PatrolEngine
    from agentic_scraper.scanner.patrol_scheduler import PatrolScheduler
    from agentic_scraper.sites.registry import SiteRegistry
    from agentic_scraper.storage.repositories.deal_repo import DealRepository
    from agentic_scraper.storage.repositories.feedback_repo import FeedbackRepository
    from agentic_scraper.storage.repositories.listing_repo import ListingRepository
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
        intents.reactions = True
        intents.voice_states = True  # Required for voice channel events
        intents.members = True  # Required for resolving user display names in voice

        super().__init__(
            command_prefix=config.discord_command_prefix,
            intents=intents,
        )

        self.config = config
        self.patrol_engine: PatrolEngine | None = None
        self.patrol_scheduler: PatrolScheduler | None = None
        self.notifier: DealNotifier | None = None
        self.site_registry: SiteRegistry | None = None
        self.watchlist_repo: WatchlistRepository | None = None
        self.deal_repo: DealRepository | None = None
        self.listing_repo: ListingRepository | None = None
        self.poob_brain: PoobBrain | None = None
        self.feedback_repo: FeedbackRepository | None = None
        self.voice_session_factory: object | None = None  # Callable[[VoiceClient], VoiceSession]
        self._cogs_loaded = False

    async def _load_cogs(self) -> None:
        """Load all cog extensions. Called from on_ready (Pycord compatibility)."""
        from agentic_scraper.discord_bot.cogs.admin_cog import AdminCog
        from agentic_scraper.discord_bot.cogs.scanning_cog import ScanningCog
        from agentic_scraper.discord_bot.cogs.search_cog import SearchCog
        from agentic_scraper.discord_bot.cogs.watchlist_cog import WatchlistCog

        if self.watchlist_repo:
            self.add_cog(WatchlistCog(bot=self, watchlist_repo=self.watchlist_repo))

        if self.patrol_scheduler and self.site_registry:
            self.add_cog(ScanningCog(
                bot=self,
                scheduler=self.patrol_scheduler,
                registry=self.site_registry,
            ))

        if self.site_registry:
            self.add_cog(AdminCog(bot=self, registry=self.site_registry))

        self.add_cog(SearchCog(
            bot=self,
            deal_repo=self.deal_repo,
            listing_repo=self.listing_repo,
        ))

        if self.poob_brain:
            from agentic_scraper.discord_bot.agent_handler import AgentMessageHandler

            self.add_cog(AgentMessageHandler(bot=self, brain=self.poob_brain))

        voice_cog = None
        if self.voice_session_factory and self.config.voice_enabled:
            from agentic_scraper.discord_bot.cogs.voice_cog import VoiceCog

            voice_cog = VoiceCog(bot=self, session_factory=self.voice_session_factory)
            self.add_cog(voice_cog)

        # Music cog — requires voice to be enabled and bot in a voice channel
        if self.config.music_enabled and self.config.voice_enabled:
            from agentic_scraper.discord_bot.cogs.music_cog import MusicCog

            # Helper to look up voice sessions from the VoiceCog
            def _get_voice_session(guild_id: int):
                if voice_cog:
                    return voice_cog._get_session(guild_id)
                return None

            music_cog = MusicCog(
                bot=self,
                config=self.config,
                poob_brain=self.poob_brain,
                get_voice_session=_get_voice_session,
            )
            self.add_cog(music_cog)

            # Set guild ID context on PoobBrain for voice routing
            if self.poob_brain and voice_cog:
                # The guild ID gets set dynamically per voice session
                log.info("Music cog wired to PoobBrain and VoiceCog")

        if self.notifier and self.feedback_repo:
            from agentic_scraper.discord_bot.cogs.feedback_cog import FeedbackCog

            self.add_cog(FeedbackCog(
                bot=self,
                notifier=self.notifier,
                feedback_repo=self.feedback_repo,
                deal_repo=self.deal_repo,
                listing_repo=self.listing_repo,
            ))

        log.info("All cogs loaded")

    async def on_ready(self) -> None:
        """Called when the bot has connected to Discord."""
        # Load cogs on first ready (Pycord doesn't have setup_hook)
        if not self._cogs_loaded:
            self._cogs_loaded = True
            await self._load_cogs()

        log.info("Bot is ready", user=str(self.user), guilds=len(self.guilds))

        # Wire the deals notification channel and bot reference
        if self.notifier:
            self.notifier.set_bot(self)
            if self.config.discord_deals_channel_id:
                channel = self.get_channel(self.config.discord_deals_channel_id)
                if channel is not None:
                    self.notifier.set_channel(channel)
                    log.info("Deals notification channel set", channel=channel.name)
                else:
                    log.warning(
                        "Deals channel not found — deal notifications will be skipped",
                        channel_id=self.config.discord_deals_channel_id,
                    )

        # Patrol no longer auto-starts — user must request it via !scan or "scan"
