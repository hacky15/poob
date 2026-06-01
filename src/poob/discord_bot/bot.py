"""ScraperBot - main Discord bot class."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord.ext import commands

from poob.utils.logging import get_logger

if TYPE_CHECKING:
    from poob.brain.poob import PoobBrain
    from poob.config import AppConfig
    from poob.discord_bot.notifier import DealNotifier
    from poob.scanner.patrol_engine import PatrolEngine
    from poob.scanner.patrol_scheduler import PatrolScheduler
    from poob.sites.registry import SiteRegistry
    from poob.storage.repositories.deal_repo import DealRepository
    from poob.storage.repositories.feedback_repo import FeedbackRepository
    from poob.storage.repositories.listing_repo import ListingRepository
    from poob.storage.repositories.watchlist_repo import WatchlistRepository

log = get_logger("discord.bot")


class ScraperBot(commands.Bot):
    """Discord bot for Poob.

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
        self.scan_log_repo: object | None = None  # ScanLogRepository — wired in main.py; consumed by AdminCog.!logs
        self.playlist_repo: object | None = None  # GuildPlaylistsRepository — wired in main.py
        self.spotify_resolver: object | None = None  # SpotifyPlaylistResolver — wired in main.py
        self.lyrics_resolver: object | None = None  # LyricsResolver — wired in main.py
        self.voice_session_factory: object | None = None  # Callable[[VoiceClient], VoiceSession]
        self._cogs_loaded = False
        self._owner_notified = False

    async def _load_cogs(self) -> None:
        """Load all cog extensions. Called from on_ready (Pycord compatibility)."""
        from poob.discord_bot.cogs.admin_cog import AdminCog
        from poob.discord_bot.cogs.scanning_cog import ScanningCog
        from poob.discord_bot.cogs.search_cog import SearchCog
        from poob.discord_bot.cogs.watchlist_cog import WatchlistCog

        if self.watchlist_repo:
            self.add_cog(WatchlistCog(bot=self, watchlist_repo=self.watchlist_repo))

        if self.patrol_scheduler and self.site_registry:
            self.add_cog(ScanningCog(
                bot=self,
                scheduler=self.patrol_scheduler,
                registry=self.site_registry,
            ))

        if self.site_registry:
            self.add_cog(AdminCog(
                bot=self,
                registry=self.site_registry,
                scan_log_repo=self.scan_log_repo,
            ))

        self.add_cog(SearchCog(
            bot=self,
            deal_repo=self.deal_repo,
            listing_repo=self.listing_repo,
        ))

        if self.poob_brain:
            from poob.discord_bot.agent_handler import AgentMessageHandler

            self.add_cog(AgentMessageHandler(bot=self, brain=self.poob_brain))

        voice_cog = None
        if self.voice_session_factory and self.config.voice_enabled:
            from poob.discord_bot.cogs.voice_cog import VoiceCog

            voice_cog = VoiceCog(bot=self, session_factory=self.voice_session_factory)
            self.add_cog(voice_cog)

        # Music cog — requires voice to be enabled and bot in a voice channel
        if self.config.music_enabled and self.config.voice_enabled:
            from poob.discord_bot.cogs.music_cog import MusicCog

            # Helper to look up voice sessions from the VoiceCog
            def _get_voice_session(guild_id: int):
                if voice_cog:
                    return voice_cog._get_session(guild_id)
                return None

            # Helper to set up a full voice session (STT + wake word) on
            # an already-connected VC. Used by music_cog's auto-join so
            # text-channel music requests get the same listening pipeline
            # /join would have wired up. See docs/incidents/auto-join-
            # missed-listening-setup for why this hand-off exists.
            async def _setup_voice_session(vc, channel, is_stage=False):
                if voice_cog:
                    return await voice_cog.setup_session_for_vc(
                        vc, channel, is_stage=is_stage, play_entrance=False,
                    )
                return None

            music_cog = MusicCog(
                bot=self,
                config=self.config,
                poob_brain=self.poob_brain,
                get_voice_session=_get_voice_session,
                setup_voice_session=_setup_voice_session,
                playlist_repo=self.playlist_repo,
                spotify_resolver=self.spotify_resolver,
                lyrics_resolver=self.lyrics_resolver,
            )
            self.add_cog(music_cog)

            # Set guild ID context on PoobBrain for voice routing
            if self.poob_brain and voice_cog:
                # The guild ID gets set dynamically per voice session
                log.info("Music cog wired to PoobBrain and VoiceCog")

        if self.notifier and self.feedback_repo:
            from poob.discord_bot.cogs.feedback_cog import FeedbackCog

            self.add_cog(FeedbackCog(
                bot=self,
                notifier=self.notifier,
                feedback_repo=self.feedback_repo,
                deal_repo=self.deal_repo,
                listing_repo=self.listing_repo,
            ))

        log.info("All cogs loaded")

    async def _sync_slash_commands(self) -> None:
        """Register slash commands with Discord after cogs are loaded.

        Pycord's automatic sync runs in ``on_connect``, which fires before
        ``on_ready`` — and we only add cogs in ``on_ready``. That means the
        automatic sync sees zero slash commands and registers nothing.
        This method catches that ordering gap.

        Syncs per-guild (via every guild the bot is currently in) so commands
        appear instantly. Global sync would work too, but Discord takes up to
        one hour to propagate global commands — unusable for iterative work.
        """
        try:
            guild_ids = [g.id for g in self.guilds]
            if guild_ids:
                await self.sync_commands(guild_ids=guild_ids)
                log.info(
                    "Synced slash commands per-guild",
                    guilds=len(guild_ids),
                )
            else:
                # No guilds yet — fall back to global sync. Not great (slow),
                # but the bot will get per-guild sync on next restart.
                await self.sync_commands()
                log.info("Synced slash commands globally (no guilds cached)")
        except Exception as exc:
            log.error(
                "Failed to sync slash commands",
                error=str(exc)[:150],
            )

    def _register_persistent_views(self) -> None:
        """Register Views with ``timeout=None`` so buttons survive restarts.

        Each persistent view is keyed by the stable ``custom_id`` strings
        assigned to its buttons (``poob:music:skip``, etc.). Pycord maps
        incoming component interactions to registered views by those ids.
        """
        try:
            music_cog = self.get_cog("Music")
            if music_cog is not None:
                from poob.discord_bot.music_ui import MusicControlsView

                self.add_view(MusicControlsView(music_cog.handle_music_request))
                log.info("Registered persistent MusicControlsView")
        except Exception as exc:
            log.warning("Persistent view registration failed", error=str(exc)[:120])

    async def on_ready(self) -> None:
        """Called when the bot has connected to Discord."""
        # Load cogs on first ready (Pycord doesn't have setup_hook)
        if not self._cogs_loaded:
            self._cogs_loaded = True
            await self._load_cogs()
            # Re-register persistent views so button custom_ids route correctly
            # after bot restarts. Without this, buttons on previously-posted
            # now-playing embeds fail with "This interaction failed".
            self._register_persistent_views()
            # Sync slash commands. Pycord's auto-sync in on_connect fires
            # BEFORE on_ready, so at that point no cogs have been added and
            # no slash commands exist to register. We must sync again here,
            # after _load_cogs, or /join and /leave never reach Discord.
            #
            # Sync per-guild (not global) so commands appear INSTANTLY.
            # Global sync takes up to 1 hour to propagate. Per-guild is
            # cached on our side via self.guilds.
            await self._sync_slash_commands()

        log.info("Bot is ready", user=str(self.user), guilds=len(self.guilds))

        # Auto-rejoin any voice channels Poob was in before this restart, so a
        # redeploy "just works" without a fresh /join. Once per process,
        # best-effort. See docs/decisions/voice-auto-rejoin-on-restart.md.
        voice_cog = self.get_cog("Voice")
        if voice_cog is not None:
            try:
                await voice_cog.restore_sessions()
            except Exception as exc:
                log.warning("Voice auto-rejoin failed", error=str(exc)[:120])

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

        # DM the owner once per process that we're alive + which version is running.
        if not self._owner_notified and self.config.discord_owner_user_id:
            self._owner_notified = True
            await self._notify_owner_alive()

    async def on_guild_join(self, guild: discord.Guild) -> None:
        """Sync slash commands to a guild the bot was added to mid-session.

        ``_sync_slash_commands`` only runs once on ``on_ready``, so a guild
        that adds the bot AFTER startup gets no commands until the next
        restart. This handler closes that gap. See
        [[slash-command-sync-on-ready]] for the original ordering bug and
        [[pycord-auto-sync-commands-fires-before-cogs]] for the gotcha.
        """
        try:
            await self.sync_commands(guild_ids=[guild.id])
            log.info(
                "Synced slash commands to newly-joined guild",
                guild_id=guild.id, guild_name=guild.name,
            )
        except Exception as exc:
            log.error(
                "Failed to sync slash commands to new guild",
                guild_id=guild.id, error=str(exc)[:150],
            )

    async def _notify_owner_alive(self) -> None:
        from poob import __version__

        owner_id = self.config.discord_owner_user_id
        sha = (self.config.git_sha or "dev")[:7]
        try:
            user = await self.fetch_user(owner_id)
            await user.send(f"Poob is alive 🍑 — v{__version__} (`{sha}`)")
            log.info("Startup DM sent to owner", owner=owner_id, sha=sha)
        except Exception as exc:
            # Owner DMs disabled, user not found, or Discord blocked the DM — non-fatal.
            log.warning("Could not DM owner on startup", owner=owner_id, error=str(exc))
