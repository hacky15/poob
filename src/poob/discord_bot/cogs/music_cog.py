"""Music cog — YouTube playback with a single structured-input contract.

Every user input path funnels into ``handle_music_request(..., tool_args={...})``:

- Voice: STT → PoobBrain → LLM ``music_assistant`` tool call → handler
- Text @mention: PoobBrain → same LLM → handler
- Button (⏭/⏸/🔁 on the now-playing embed): view callback → handler

The LLM's only job is classifying unstructured speech/text into structured
``tool_args``. Buttons skip the LLM because their intent is already
classified — identical contract, no divergent code paths. The handler
refreshes ``PoobBrain._music_playing_info`` from live player state on every
call, keeping brain context in sync across modalities.

No prefix commands. The system owns one entry point per guild.
"""

from __future__ import annotations

import asyncio
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

    async def _auto_join_requester_vc(
        self, guild: discord.Guild, user_id: int,
    ) -> discord.VoiceClient | None:
        """Join the requester's voice channel — and only the requester's.

        Used when a text-channel @mention music request arrives but the bot
        isn't in any VC yet. The invariant is: Poob plays music where the
        requester is, never where they aren't. If the requester isn't in a
        VC, we return None and the caller text-replies to join first.

        Lightweight join — no VoiceSession (STT/recording). For full voice
        interaction the user invokes ``/join``. The music player only needs
        a connected VoiceClient to stream audio.
        """
        member = guild.get_member(user_id)
        if not member or not member.voice or not member.voice.channel:
            return None

        channel = member.voice.channel
        try:
            vc = await channel.connect(timeout=15.0)
            log.info(
                "Auto-joined requester's voice channel",
                guild=guild.id,
                channel=channel.name,
                requester=user_id,
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
        # Keep brain's music-playing context fresh for THIS guild.
        # Goes through the per-guild helper so other guilds' info
        # stays in their own slots (multi-guild isolation).
        if self._poob_brain and guild_id:
            player = self._get_player(guild_id)
            if player and player.current_track:
                track = player.current_track
                self._poob_brain._set_music_playing_info(
                    guild_id,
                    f"{track.title} [{track.duration_str}]",
                )
            else:
                self._poob_brain._set_music_playing_info(guild_id, "")

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
            # Not in a VC yet — join the requester's channel (and ONLY theirs).
            vc = await self._auto_join_requester_vc(guild, user_id)
            if not vc:
                return (
                    "you gotta be in a voice channel for me to play anything. "
                    "hop in and try again."
                )
        else:
            # Already in a VC. Requester must be in THE SAME one, otherwise
            # music would play where they can't hear it. This is the invariant
            # that keeps the UX coherent across voice, text, and button input.
            member = guild.get_member(user_id)
            in_same_vc = (
                member is not None
                and member.voice is not None
                and member.voice.channel == vc.channel
            )
            # Structured UI actions (button clicks) pass structured tool_args
            # and only appear on the now-playing embed — if the clicker isn't
            # in the VC they see an ephemeral error from the view, so we don't
            # re-check here. The gate only applies to "play"/"queue"-style
            # requests where the action creates new audio output.
            tool_action = (tool_args or {}).get("action", "")
            starts_new_audio = tool_action in ("", "play")
            if starts_new_audio and not in_same_vc:
                return (
                    "nah, you gotta be in the voice channel with me to request "
                    "tunes. can't serenade an empty seat."
                )

        player = self._get_or_create_player(vc, guild_id)
        member = guild.get_member(user_id)
        requester_name = member.display_name if member else f"User-{user_id}"

        # Structured input is the only contract now. Voice/text @mention
        # paths route through the LLM which produces tool_args; button
        # callbacks build tool_args directly. Text command fallbacks are
        # deleted — see docs/technical_notes.md "One-Handler Music Contract".
        action = (tool_args or {}).get("action", "")
        query = (tool_args or {}).get("query", "")
        value = (tool_args or {}).get("value")

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
        if not query or len(query) < 2:
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
    # Now-playing UI (embed + persistent buttons)
    # ------------------------------------------------------------------

    def build_now_playing_message(
        self, guild_id: int,
    ) -> tuple[discord.Embed, discord.ui.View] | None:
        """Render the current track as (embed, persistent-view) for posting.

        Called by AgentMessageHandler after a music action completes, to
        attach the now-playing card to the text channel. Returns None when
        nothing is playing (e.g. the action was a skip-with-empty-queue).

        The view's button callbacks funnel through ``handle_music_request``
        with structured ``tool_args`` — the same entry point voice/text
        @mentions use. No divergent code paths.
        """
        from poob.discord_bot.music_ui import build_now_playing_message as _build

        player = self._get_player(guild_id)
        if player is None:
            return None
        return _build(player, self.handle_music_request)

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
