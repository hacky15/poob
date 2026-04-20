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

from poob.music.player import GuildMusicPlayer
from poob.music.queue import LoopMode, Track
from poob.music.ytdl import AsyncYTDL
from poob.utils.logging import get_logger

if TYPE_CHECKING:
    from poob.config import AppConfig
    from poob.brain.poob import PoobBrain
    from poob.voice.session import VoiceSession

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

    async def _auto_join_voice(self, guild: discord.Guild) -> discord.VoiceClient | None:
        """Auto-join the most populated voice channel in the guild.

        Used when a music request arrives from a text channel but the bot
        isn't in any voice channel yet. Picks the voice channel with the
        most human members so Poob plays where people actually are.

        This is a lightweight join — no VoiceSession (STT/recording) is
        created. Users can ``!join`` for full voice interaction. The music
        player only needs a connected VoiceClient to stream audio.

        Returns:
            The connected VoiceClient, or None if no channels are available.
        """
        best_channel: discord.VoiceChannel | None = None
        best_count = -1

        for channel in guild.voice_channels:
            humans = sum(1 for m in channel.members if not m.bot)
            if humans > best_count:
                best_count = humans
                best_channel = channel

        if not best_channel:
            return None

        try:
            vc = await best_channel.connect(timeout=15.0)
            log.info(
                "Auto-joined voice for music",
                guild=guild.id,
                channel=best_channel.name,
                humans=best_count,
            )
            return vc
        except Exception as exc:
            log.error("Auto-join failed", guild=guild.id, error=str(exc)[:100])
            return None

    # ------------------------------------------------------------------
    # Agentic handler (called by PoobBrain)
    # ------------------------------------------------------------------

    async def handle_music_request(
        self,
        request: str,
        user_id: int,
        guild_id: int,
        *,
        voice: bool = False,
        tool_args: dict | None = None,
    ) -> str:
        """Handle a structured music request from PoobBrain.

        When tool_args is provided (from LLM structured tool calling), the
        action is already classified — no parsing needed. Falls back to raw
        text parsing only for text commands (!play) that bypass the LLM.

        Args:
            request: The user's raw message (used for play queries as fallback).
            user_id: Discord user ID.
            guild_id: Discord guild ID (0 if not in a guild/voice).
            voice: If True, defer playback start (Poob speaks first).
            tool_args: Structured args from LLM tool call:
                       {action: str, query?: str, value?: int}
        """
        # Keep brain's music-playing context fresh
        if self._poob_brain:
            player = self._get_player(guild_id) if guild_id else None
            if player and player.current_track:
                self._poob_brain._music_playing_info = (
                    f"{player.current_track.title} "
                    f"[{player.current_track.duration_str}]"
                )
            else:
                self._poob_brain._music_playing_info = ""

        # Resolve guild — prefer explicit guild_id (text channels pass this),
        # fall back to finding the user in a voice channel (legacy path).
        guild = self.bot.get_guild(guild_id) if guild_id else None
        if not guild:
            for g in self.bot.guilds:
                member = g.get_member(user_id)
                if member and member.voice and member.voice.channel:
                    guild = g
                    guild_id = g.id
                    break
        if not guild:
            return "I can't figure out which server you're in."

        vc = guild.voice_client
        if not vc or not vc.is_connected():
            vc = await self._auto_join_voice(guild)
            if not vc:
                return "No voice channels available to join."

        player = self._get_or_create_player(vc, guild_id)
        member = guild.get_member(user_id)
        requester_name = member.display_name if member else f"User-{user_id}"

        # Extract structured action from LLM tool args.
        # If no tool_args (text command fallback), parse from raw request.
        action = (tool_args or {}).get("action", "")
        query = (tool_args or {}).get("query", "")
        value = (tool_args or {}).get("value")

        if not action:
            # Fallback: text commands (!play, !skip etc.) don't go through LLM
            action, query, value = self._parse_intent_fallback(request)

        log.info("music.action", action=action, query=query[:60] if query else "",
                 value=value, user=user_id)

        # --- Action dispatch (no regex, no keyword matching) ---

        if action == "skip":
            if player.current_track:
                skipped = player.current_track.title
                await player.skip()
                return f"[SILENT]Skipped {skipped}."
            return "[SILENT]Nothing is playing to skip."

        if action == "pause":
            if player.pause():
                return "[SILENT]Music paused."
            return "[SILENT]Nothing is playing to pause."

        if action == "resume":
            if player.resume():
                return "[SILENT]Music resumed."
            return "[SILENT]Nothing is paused."

        if action == "stop":
            await player.stop()
            return "[SILENT]Music stopped and queue cleared."

        if action == "now_playing":
            track = player.current_track
            if track:
                return f"[SILENT]Now playing: {track.title} [{track.duration_str}], requested by {track.requester_name}."
            return "[SILENT]Nothing is playing right now."

        if action == "queue":
            return f"[SILENT]{player.queue.format_queue()}"

        if action == "shuffle":
            shuffled = player.queue.toggle_shuffle()
            return f"[SILENT]Shuffle {'enabled' if shuffled else 'disabled'}."

        if action == "loop":
            mode = player.queue.cycle_loop_mode()
            mode_names = {
                LoopMode.OFF: "off",
                LoopMode.LOOP_ONE: "looping current track",
                LoopMode.LOOP_QUEUE: "looping entire queue",
            }
            return f"[SILENT]Loop mode: {mode_names[mode]}."

        if action == "volume":
            vol = value if value is not None else 50
            player.volume = max(0.0, min(2.0, vol / 100.0))
            return f"[SILENT]Music volume set to {vol}%."

        if action == "volume_down":
            if value is not None:
                player.volume = max(0.0, min(2.0, value / 100.0))
                return f"[SILENT]Music volume set to {value}%."
            player.volume = max(0.05, player.volume - 0.2)
            return f"[SILENT]Volume lowered to {int(player.volume * 100)}%."

        if action == "volume_up":
            if value is not None:
                player.volume = max(0.0, min(2.0, value / 100.0))
                return f"[SILENT]Music volume set to {value}%."
            player.volume = min(2.0, player.volume + 0.2)
            return f"[SILENT]Volume raised to {int(player.volume * 100)}%."

        # Default: "play" action (or unrecognized action treated as play)
        if not query:
            # Extract query from raw request as fallback
            query = self._extract_play_query(request)

        # Reject queries that are just the command word itself (no actual song name)
        bare_commands = {"play", "queue", "put on", "throw on", "add", "play me"}
        if not query or len(query) < 2 or query.lower().strip(" .!?,") in bare_commands:
            return "What do you want me to play?"

        # Playlist URL
        if "list=" in query or "/playlist" in query:
            playlist_title, tracks = await self._ytdl.get_playlist_tracks(
                query, requester_id=user_id, requester_name=requester_name,
            )
            if not tracks:
                return "Couldn't load that playlist."
            count = await player.play_many(tracks, deferred=voice)
            return f"Queued {count} tracks from {playlist_title}."

        # Single track search
        track = await self._ytdl.search(
            query, requester_id=user_id, requester_name=requester_name,
        )
        if not track:
            return f"Couldn't find anything for '{query}'."

        already_playing = player.is_playing
        await player.play(track, deferred=voice)

        if not already_playing:
            return f"Playing {track.title} [{track.duration_str}]."
        pos = player.queue.size
        return f"Queued {track.title} [{track.duration_str}] at position {pos}."

    # ------------------------------------------------------------------
    # Fallback parsing (text commands only — LLM path never hits this)
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_intent_fallback(request: str) -> tuple[str, str, int | None]:
        """Parse intent from raw text when no LLM tool_args are available.

        Used only for text commands (!play, !skip) that bypass the LLM.
        Returns (action, query, value).
        """
        req_lower = request.lower().strip()

        if any(kw in req_lower for kw in ["skip", "next song", "next track"]):
            return "skip", "", None
        if "pause" in req_lower:
            return "pause", "", None
        if "resume" in req_lower or "unpause" in req_lower:
            return "resume", "", None
        if any(kw in req_lower for kw in [
            "stop music", "stop the music", "stop playing", "stop this",
            "stop it", "clear queue",
        ]) or req_lower in ("stop", "stop stop"):
            return "stop", "", None
        if any(kw in req_lower for kw in [
            "what's playing", "whats playing", "now playing", "what song",
        ]):
            return "now_playing", "", None
        if "shuffle" in req_lower:
            return "shuffle", "", None
        if "loop" in req_lower:
            return "loop", "", None

        vol_match = re.search(r"volume\s*[,.]?\s*(?:to\s+)?(\d+)", req_lower)
        if vol_match:
            return "volume", "", int(vol_match.group(1))
        if any(kw in req_lower for kw in ["turn down", "lower", "quieter"]):
            return "volume_down", "", None
        if any(kw in req_lower for kw in ["turn up", "louder"]):
            return "volume_up", "", None

        # Default to play
        query = MusicCog._extract_play_query(request)
        return "play", query, None

    @staticmethod
    def _extract_play_query(request: str) -> str:
        """Extract the song/artist query from a play request."""
        # Strip wake word prefix
        cleaned = re.sub(
            r'^(?:hey[,.]?\s*)?(?:poob|poop|pub|boob)[,.]?\s*',
            '', request, flags=re.IGNORECASE,
        ).strip()
        if not cleaned:
            cleaned = request

        req_lower = cleaned.lower()
        # Longer prefixes first to prevent "play " matching before "play me "
        for prefix in [
            "play the song called ", "play the song ", "can you play ",
            "play some ", "play me ", "queue up ", "throw on ", "put on ",
            "play ", "queue ", "add ",
        ]:
            idx = req_lower.find(prefix)
            if idx != -1:
                return cleaned[idx + len(prefix):].strip()
        return cleaned

    # ------------------------------------------------------------------
    # Text commands (fallbacks)
    # ------------------------------------------------------------------

    @commands.command(name="play", aliases=["p"])
    async def play_cmd(self, ctx: commands.Context, *, query: str) -> None:
        """Play a song or add it to the queue.

        Usage: !play <url or search query>
        """
        async with ctx.typing():
            result = await self.handle_music_request(
                f"play {query}",
                ctx.author.id,
                ctx.guild.id,
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
