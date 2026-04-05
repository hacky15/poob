"""Music cog — YouTube playback with agentic voice control.

Provides both traditional text commands (!play, !skip, etc.) as fallbacks
and an agentic handler that PoobBrain routes voice/text music requests to.
The agentic handler parses natural language intents and delegates to the
same underlying GuildMusicPlayer.

Architecture:
    Voice: User speaks → STT → PoobBrain → music_assistant tool → handle_music_request()
    Text:  User types !play xyz → play() command
    Both paths use the same GuildMusicPlayer per guild.
"""

from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING

import discord
from discord.ext import commands

from agentic_scraper.music.player import GuildMusicPlayer
from agentic_scraper.music.queue import LoopMode, Track
from agentic_scraper.music.ytdl import AsyncYTDL
from agentic_scraper.utils.logging import get_logger

if TYPE_CHECKING:
    from agentic_scraper.config import AppConfig
    from agentic_scraper.brain.poob import PoobBrain
    from agentic_scraper.voice.session import VoiceSession

log = get_logger("discord.music_cog")


class MusicCog(commands.Cog, name="Music"):
    """YouTube music player with real-time TTS mixing.

    Creates one GuildMusicPlayer per guild. When the bot is in a voice channel,
    music plays through a MixingAudioSource that allows Poob's TTS to overlay
    without interrupting the music stream.
    """

    def __init__(
        self,
        bot: commands.Bot,
        config: AppConfig,
        poob_brain: PoobBrain | None = None,
        get_voice_session=None,  # Callable[[int], VoiceSession | None]
    ) -> None:
        self.bot = bot
        self.config = config
        self._poob_brain = poob_brain
        self._get_voice_session = get_voice_session  # guild_id → VoiceSession

        self._ytdl = AsyncYTDL(cookie_file=config.music_ytdl_cookie_file)
        self._players: dict[int, GuildMusicPlayer] = {}  # guild_id → player

        # Register agentic handler with PoobBrain
        if poob_brain is not None:
            poob_brain.set_music_handler(self.handle_music_request)

    # ------------------------------------------------------------------
    # Player management
    # ------------------------------------------------------------------

    def _get_player(self, guild_id: int) -> GuildMusicPlayer | None:
        """Get existing player for a guild."""
        player = self._players.get(guild_id)
        if player and player.voice_client.is_connected():
            return player
        return None

    def _get_or_create_player(self, vc: discord.VoiceClient, guild_id: int) -> GuildMusicPlayer:
        """Get or create a player for a guild."""
        existing = self._get_player(guild_id)
        if existing and existing.voice_client == vc:
            return existing

        # Create new player
        player = GuildMusicPlayer(
            voice_client=vc,
            ytdl=self._ytdl,
            volume=self.config.music_volume,
            idle_timeout=self.config.music_idle_timeout,
        )
        self._players[guild_id] = player

        # Wire up to voice session so TTS uses overlay
        self._link_voice_session(guild_id, player)

        return player

    def _link_voice_session(self, guild_id: int, player: GuildMusicPlayer) -> None:
        """Link the music player to the voice session for TTS overlay."""
        if self._get_voice_session:
            session = self._get_voice_session(guild_id)
            if session:
                session.music_player = player
                log.info("Music player linked to voice session", guild=guild_id)

    async def _destroy_player(self, guild_id: int) -> None:
        """Destroy a guild's music player and unlink from voice session."""
        player = self._players.pop(guild_id, None)
        if player:
            await player.destroy()
        # Unlink from voice session
        if self._get_voice_session:
            session = self._get_voice_session(guild_id)
            if session:
                session.music_player = None

    # ------------------------------------------------------------------
    # Agentic handler (called by PoobBrain)
    # ------------------------------------------------------------------

    async def handle_music_request(
        self,
        request: str,
        user_id: int,
        guild_id: int,
    ) -> str:
        """Handle a natural language music request from PoobBrain.

        Parses intent from the request and delegates to the appropriate
        player action. Returns a response string for Poob to relay.

        Args:
            request: The user's natural language request.
            user_id: Discord user ID.
            guild_id: Discord guild ID (0 if not in a guild/voice).
        """
        req_lower = request.lower().strip()

        # Resolve guild — try to find the guild and voice client
        guild = self.bot.get_guild(guild_id) if guild_id else None
        if not guild:
            # Try to find from user's voice state
            for g in self.bot.guilds:
                member = g.get_member(user_id)
                if member and member.voice and member.voice.channel:
                    guild = g
                    guild_id = g.id
                    break

        if not guild:
            return "You need to be in a voice channel for music."

        vc = guild.voice_client
        if not vc or not vc.is_connected():
            return "I'm not in a voice channel. Use !join first."

        # Get or create player
        player = self._get_or_create_player(vc, guild_id)

        # Resolve requester name
        member = guild.get_member(user_id)
        requester_name = member.display_name if member else f"User-{user_id}"

        # --- Intent parsing ---
        # Skip / next
        if any(kw in req_lower for kw in ["skip", "next song", "next track"]):
            if player.current_track:
                skipped = player.current_track.title
                await player.skip()
                return f"Skipped {skipped}."
            return "Nothing is playing to skip."

        # Pause
        if any(kw in req_lower for kw in ["pause music", "pause the music", "pause"]):
            if player.pause():
                return "Music paused."
            return "Nothing is playing to pause."

        # Resume
        if any(kw in req_lower for kw in ["resume music", "resume", "unpause"]):
            if player.resume():
                return "Music resumed."
            return "Nothing is paused."

        # Stop
        if any(kw in req_lower for kw in ["stop music", "stop the music", "clear queue"]):
            await player.stop()
            return "Music stopped and queue cleared."

        # Now playing
        if any(kw in req_lower for kw in [
            "what's playing", "whats playing", "now playing",
            "what song", "current song", "what is this",
        ]):
            track = player.current_track
            if track:
                return f"Now playing: {track.title} [{track.duration_str}], requested by {track.requester_name}."
            return "Nothing is playing right now."

        # Show queue
        if any(kw in req_lower for kw in ["show queue", "list queue", "what's in the queue"]):
            return player.queue.format_queue()

        # Shuffle
        if "shuffle" in req_lower:
            shuffled = player.queue.toggle_shuffle()
            return f"Shuffle {'enabled' if shuffled else 'disabled'}."

        # Loop
        if "loop" in req_lower:
            mode = player.queue.cycle_loop_mode()
            mode_names = {
                LoopMode.OFF: "off",
                LoopMode.LOOP_ONE: "looping current track",
                LoopMode.LOOP_QUEUE: "looping entire queue",
            }
            return f"Loop mode: {mode_names[mode]}."

        # Volume
        vol_match = re.search(r"volume\s+(\d+)", req_lower)
        if vol_match:
            vol = int(vol_match.group(1))
            player.volume = vol / 100.0
            return f"Music volume set to {vol}%."

        # Play / queue — the main path
        # Extract the query (everything after "play", "queue", "put on", etc.)
        query = request
        for prefix in [
            "play ", "queue ", "put on ", "throw on ", "play me ",
            "queue up ", "add ", "play some ",
        ]:
            idx = req_lower.find(prefix)
            if idx != -1:
                query = request[idx + len(prefix):].strip()
                break

        if not query or len(query) < 2:
            return "What do you want me to play?"

        # Check if it's a playlist URL
        if "list=" in query or "/playlist" in query:
            playlist_title, tracks = await self._ytdl.get_playlist_tracks(
                query,
                requester_id=user_id,
                requester_name=requester_name,
            )
            if not tracks:
                return f"Couldn't load that playlist."
            count = await player.play_many(tracks)
            return f"Queued {count} tracks from {playlist_title}."

        # Single track search
        track = await self._ytdl.search(
            query,
            requester_id=user_id,
            requester_name=requester_name,
        )
        if not track:
            return f"Couldn't find anything for '{query}'."

        await player.play(track)

        if player.queue.size == 0 and player.current_track == track:
            return f"Now playing: {track.title} [{track.duration_str}]."
        else:
            pos = player.queue.size
            return f"Queued {track.title} [{track.duration_str}] at position {pos}."

    # ------------------------------------------------------------------
    # Text commands (fallbacks)
    # ------------------------------------------------------------------

    @commands.command(name="play", aliases=["p"])
    async def play_cmd(self, ctx: commands.Context, *, query: str) -> None:
        """Play a song or add it to the queue.

        Usage: !play <url or search query>
        """
        if not ctx.author.voice or not ctx.author.voice.channel:
            await ctx.send("You need to be in a voice channel.")
            return

        guild = ctx.guild
        vc = guild.voice_client
        if not vc or not vc.is_connected():
            await ctx.send("I'm not in a voice channel. Use `!join` first.")
            return

        async with ctx.typing():
            result = await self.handle_music_request(
                f"play {query}",
                ctx.author.id,
                guild.id,
            )
        await ctx.send(result)

    @commands.command(name="skip", aliases=["s", "next"])
    async def skip_cmd(self, ctx: commands.Context) -> None:
        """Skip the current track."""
        result = await self.handle_music_request("skip", ctx.author.id, ctx.guild.id)
        await ctx.send(result)

    @commands.command(name="mpause")
    async def pause_cmd(self, ctx: commands.Context) -> None:
        """Pause music playback."""
        result = await self.handle_music_request("pause music", ctx.author.id, ctx.guild.id)
        await ctx.send(result)

    @commands.command(name="mresume")
    async def resume_cmd(self, ctx: commands.Context) -> None:
        """Resume music playback."""
        result = await self.handle_music_request("resume music", ctx.author.id, ctx.guild.id)
        await ctx.send(result)

    @commands.command(name="mstop")
    async def stop_cmd(self, ctx: commands.Context) -> None:
        """Stop music and clear the queue."""
        result = await self.handle_music_request("stop music", ctx.author.id, ctx.guild.id)
        await ctx.send(result)

    @commands.command(name="np", aliases=["nowplaying"])
    async def now_playing_cmd(self, ctx: commands.Context) -> None:
        """Show what's currently playing."""
        result = await self.handle_music_request("what's playing", ctx.author.id, ctx.guild.id)
        await ctx.send(result)

    @commands.command(name="queue", aliases=["q"])
    async def queue_cmd(self, ctx: commands.Context) -> None:
        """Show the music queue."""
        player = self._get_player(ctx.guild.id)
        if not player:
            await ctx.send("Nothing playing.")
            return
        await ctx.send(player.queue.format_queue())

    @commands.command(name="shuffle")
    async def shuffle_cmd(self, ctx: commands.Context) -> None:
        """Toggle queue shuffle."""
        result = await self.handle_music_request("shuffle", ctx.author.id, ctx.guild.id)
        await ctx.send(result)

    @commands.command(name="loop")
    async def loop_cmd(self, ctx: commands.Context) -> None:
        """Cycle loop mode: off → track → queue."""
        result = await self.handle_music_request("loop", ctx.author.id, ctx.guild.id)
        await ctx.send(result)

    @commands.command(name="volume", aliases=["vol"])
    async def volume_cmd(self, ctx: commands.Context, vol: int) -> None:
        """Set music volume (0-200).

        Usage: !volume 50
        """
        result = await self.handle_music_request(f"volume {vol}", ctx.author.id, ctx.guild.id)
        await ctx.send(result)

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        """Clean up music player when bot is disconnected."""
        if member.id != self.bot.user.id:
            return
        # Bot was disconnected from voice
        if before.channel and not after.channel:
            await self._destroy_player(member.guild.id)

    async def cog_unload(self) -> None:
        """Clean up all players and yt-dlp resources."""
        for guild_id in list(self._players):
            await self._destroy_player(guild_id)
        self._ytdl.close()
