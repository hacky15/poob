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


def _track_to_dict(track: Track) -> dict:
    """Project a :class:`Track` into the persisted-playlist shape.

    Stream URLs are intentionally omitted — they expire (~6 h on YouTube)
    and the player resolves them lazily at play time via :class:`AsyncYTDL`.
    See ``docs/plans/music-named-playlists.md``.
    """
    return {
        "title": track.title,
        "url": track.url,
        "identifier": track.identifier,
        "duration_seconds": int(track.duration.total_seconds()) if track.duration else None,
        "source": track.source.value if hasattr(track.source, "value") else str(track.source),
        "is_stream": track.is_stream,
    }


def _track_from_dict(
    data: dict, requester_id: int, requester_name: str,
) -> Track:
    """Rebuild a :class:`Track` from a persisted-playlist dict.

    The requester fields are populated with the loader's identity (the
    person calling ``load_playlist``), not the original saver — matches
    "loaded by X" semantics in the queue display.
    """
    from datetime import timedelta

    from poob.music.queue import TrackSource

    duration = (
        timedelta(seconds=int(data["duration_seconds"]))
        if data.get("duration_seconds") is not None
        else None
    )
    source_raw = data.get("source", "youtube")
    try:
        source = TrackSource(source_raw)
    except (KeyError, ValueError):
        source = TrackSource.YOUTUBE
    return Track(
        title=data["title"],
        url=data["url"],
        duration=duration,
        requester_id=requester_id,
        requester_name=requester_name,
        identifier=data.get("identifier"),
        source=source,
        is_stream=bool(data.get("is_stream", False)),
    )


_LYRICS_MAX_CHARS = 1800  # Discord message hard limit is 2000; leave headroom for the prefix.


def _format_lyrics_for_reply(lyrics) -> str:
    """Project :class:`ParsedLyrics` into a Discord-safe string.

    Truncates at ``_LYRICS_MAX_CHARS`` so the surrounding ``[SILENT]…``
    wrapper fits inside Discord's 2000-char limit. Appends a
    ``(truncated)`` footer when cut. Plain lyrics return as-is (single
    blob); synced lyrics get one line per entry with the timestamp
    suppressed — the wow comes from the live overlay (v2), not from
    seeing raw ``[mm:ss]`` markers in chat.
    """
    if not lyrics.lines:
        return "(no lines)"
    rendered = "\n".join(text for _ts, text in lyrics.lines if text).strip()
    if len(rendered) <= _LYRICS_MAX_CHARS:
        return rendered
    return rendered[:_LYRICS_MAX_CHARS].rsplit("\n", 1)[0] + "\n\n(truncated)"


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
        setup_voice_session=None,  # async (vc, channel, is_stage=False) -> VoiceSession | None
        playlist_repo=None,  # GuildPlaylistsRepository | None — named-playlist persistence
        spotify_resolver=None,  # SpotifyPlaylistResolver | None — Spotify URL import
        lyrics_resolver=None,  # LyricsResolver | None — synced-lyrics fetch
    ) -> None:
        self.bot = bot
        self.config = config
        self._poob_brain = poob_brain
        self._get_voice_session = get_voice_session  # guild_id → VoiceSession
        # Wire-up callback to set up STT + wake word + dual pipeline on a
        # VC the music cog connected itself. See _auto_join_requester_vc.
        self._setup_voice_session = setup_voice_session
        # Optional per-guild named-playlist store. None disables the
        # save/load/list/delete actions with a friendly soft error. See
        # docs/plans/music-named-playlists.md.
        self._playlist_repo = playlist_repo
        # Optional Spotify public-playlist URL resolver. None disables
        # the queue_spotify_playlist action with a friendly soft error.
        # See docs/plans/music-spotify-playlist-import.md.
        self._spotify_resolver = spotify_resolver
        # Optional synced-lyrics resolver. None disables the lyrics
        # action. v1 returns lyrics text inline in the [SILENT] reply;
        # the live-overlay tick loop is queued for v2 (see
        # docs/plans/music-synced-lyrics.md).
        self._lyrics_resolver = lyrics_resolver

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
        """Join the requester's voice channel and set up full listening.

        Used when a text-channel @mention music request arrives but the bot
        isn't in any VC yet. The invariant is: Poob plays music where the
        requester is, never where they aren't. If the requester isn't in a
        VC, we return None and the caller text-replies to join first.

        Once connected, hands off to VoiceCog's session setup so STT, wake
        word, and the dual pipeline come up — same as ``/join`` would have.
        Without that hand-off, the bot can play music but can't hear "Hey
        Poob, max volume" (the original auto-join skipped session setup).
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
        except Exception as exc:
            log.error("Auto-join failed", guild=guild.id, error=str(exc)[:100])
            return None

        # Wire up listening on the freshly-connected VC. No-op if voice
        # cog is unavailable; the bot still streams music either way.
        if self._setup_voice_session is not None:
            try:
                is_stage = isinstance(channel, discord.StageChannel)
                session = await self._setup_voice_session(
                    vc, channel, is_stage=is_stage,
                )
                if session is not None:
                    log.info(
                        "Auto-join wired listening pipeline",
                        guild=guild.id, channel=channel.name,
                    )
            except Exception as exc:
                log.warning(
                    "Auto-join listening setup failed — music will "
                    "still play, but bot won't hear voice commands",
                    guild=guild.id, error=str(exc)[:120],
                )
        return vc

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
        tracks_arg = (tool_args or {}).get("tracks")
        from_position = (tool_args or {}).get("from_position")
        to_position = (tool_args or {}).get("to_position")
        position = (tool_args or {}).get("position")
        effect = (tool_args or {}).get("effect")
        time_arg = (tool_args or {}).get("time")
        mode = (tool_args or {}).get("mode")
        playlist_name = (tool_args or {}).get("name")
        url_arg = (tool_args or {}).get("url")

        log.info("music.action", action=action, query=query[:60] if query else "",
                 value=value, user=user_id)

        # --- Action dispatch (no regex, no keyword matching) ---

        if action == "skip":
            if player.current_track:
                skipped = player.current_track.title
                await player.skip()
                return f"[SILENT]Skipped {skipped}."
            return "[SILENT]Nothing is playing to skip."

        if action == "previous":
            prev = await player.previous()
            if prev is None:
                return "[SILENT]Nothing in the history to go back to."
            return f"[SILENT]Back to {prev.title}."

        if action == "replay":
            cur = await player.replay()
            if cur is None:
                return "[SILENT]Nothing is playing to replay."
            return f"[SILENT]Restarting {cur.title} from the top."

        if action == "move":
            # Tool schema is 1-based for user-facing parity with the
            # queue display. Convert to 0-based for queue.move().
            if from_position is None or to_position is None:
                return "[SILENT]Move needs from_position and to_position."
            try:
                from_idx = int(from_position) - 1
                to_idx = int(to_position) - 1
            except (TypeError, ValueError):
                return "[SILENT]Move positions must be numbers."
            moved = player.queue.move(from_idx, to_idx)
            if moved is None:
                return f"[SILENT]Position out of range (queue has {player.queue.size})."
            return f"[SILENT]Moved {moved.title} to position {to_position}."

        if action == "remove":
            if position is None:
                return "[SILENT]Remove needs a position."
            try:
                idx = int(position) - 1
            except (TypeError, ValueError):
                return "[SILENT]Position must be a number."
            removed = player.queue.remove(idx)
            if removed is None:
                return f"[SILENT]Position out of range (queue has {player.queue.size})."
            return f"[SILENT]Removed {removed.title} from the queue."

        if action == "clear":
            count = player.queue.clear()
            return f"[SILENT]Cleared {count} track{'s' if count != 1 else ''} from the queue."

        if action == "autoplay":
            mode_str = (mode or "").strip().lower()
            if mode_str not in {"on", "off", "status"}:
                return "[SILENT]Autoplay mode? (on, off, status)"
            if mode_str == "status":
                state = "on" if player.autoplay_enabled else "off"
                return f"[SILENT]Autoplay is {state}."
            player.autoplay_enabled = (mode_str == "on")
            return (
                "[SILENT]Autoplay enabled."
                if player.autoplay_enabled
                else "[SILENT]Autoplay disabled."
            )

        if action == "list_effects":
            from poob.music.effects import AVAILABLE_EFFECTS
            names = [e for e in AVAILABLE_EFFECTS if e != "none"]
            pretty = ", ".join(n.replace("_", " ") for n in names)
            # Deliberately NOT [SILENT]: the user asked a question, so Poob
            # speaks the list rather than silently performing a control action.
            return (
                f"I've got {len(names)} effects you can throw on: {pretty}. "
                "Say one to apply it, or say 'none' to clear it."
            )

        if action == "apply_effect":
            if not effect:
                from poob.music.effects import AVAILABLE_EFFECTS
                return "[SILENT]Which effect? (" + ", ".join(AVAILABLE_EFFECTS) + ")"
            try:
                from poob.music.effects import EffectNotFoundError
                applied = await player.set_effect(effect)
            except EffectNotFoundError as exc:
                return f"[SILENT]{exc}"
            if applied is None:
                return f"[SILENT]Effect '{player.active_effect}' set — applies on the next track."
            if player.active_effect == "none":
                return "[SILENT]Audio effect cleared."
            return f"[SILENT]Applied {player.active_effect}."

        if action == "seek":
            if not time_arg:
                return "[SILENT]Where to? (e.g. '2:30', '+10', '-30s')"
            from poob.music.seek import SeekParseError, parse_seek_input
            try:
                parsed = parse_seek_input(time_arg)
            except SeekParseError as exc:
                return f"[SILENT]{exc}"
            result = await player.seek(parsed)
            if result is None:
                cur = player.current_track
                if cur and cur.is_stream:
                    return "[SILENT]Can't seek a live stream."
                return "[SILENT]Nothing is playing to seek within."
            track, target = result
            # Format target back into m:ss for the silent reply.
            mins, secs = divmod(int(target), 60)
            return f"[SILENT]Seeked {track.title} to {mins}:{secs:02d}."

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

        if action == "leave":
            # Cross-cog reach: VoiceCog owns the disconnect path (force-
            # disconnect cleans up the session, recording sink, etc.).
            # The button on the now-playing embed routes here; voice
            # @mention "Poob leave" also lands here via the LLM tool call.
            await player.stop()
            voice_cog = self.bot.get_cog("Voice")
            if voice_cog is not None:
                try:
                    await voice_cog._force_disconnect(guild)
                except Exception as exc:
                    log.warning("Leave: force-disconnect failed", error=str(exc)[:100])
            return "[SILENT]Left the voice channel."

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

        if action == "queue_many":
            # Multi-track in a single utterance — search each, queue the
            # resolved ones, report per-track status. See
            # docs/decisions/music-queue-many-tool.md.
            if not isinstance(tracks_arg, list) or not tracks_arg:
                return "[SILENT]queue_many needs a non-empty 'tracks' list."
            resolved: list[str] = []
            not_found: list[str] = []
            already_playing = player.is_playing
            for raw in tracks_arg:
                title = str(raw).strip()
                if len(title) < 2:
                    not_found.append(title or "(empty)")
                    continue
                track = await self._ytdl.search(
                    title, requester_id=user_id, requester_name=requester_name,
                )
                if not track:
                    not_found.append(title)
                    continue
                await player.play(track, deferred=voice)
                resolved.append(track.title)
            if not resolved:
                return f"Couldn't find any of: {', '.join(not_found[:5])}."
            head = (
                f"Queued {len(resolved)} tracks"
                if already_playing
                else f"Playing {resolved[0]}, queued {len(resolved) - 1} more"
            )
            if not_found:
                head += f". Couldn't find: {', '.join(not_found[:3])}"
                if len(not_found) > 3:
                    head += f" (+{len(not_found) - 3} more)"
            return f"{head}."

        # --- Named playlists (save / load / list / delete) ---
        # Per-guild persistent store. The repo is optional at cog-init
        # time so tests can run without a DB; production wires one in
        # via main.py. See docs/plans/music-named-playlists.md.

        if action in {"save_playlist", "load_playlist", "delete_playlist"}:
            if self._playlist_repo is None:
                return "[SILENT]Playlists aren't configured on this stack yet."
            name_clean = (playlist_name or "").strip()
            if not name_clean:
                return "[SILENT]Name your playlist?"
            guild_id_str = str(guild.id)

            if action == "save_playlist":
                upcoming = list(player.queue.upcoming)
                current = player.current_track
                tracks_to_save = ([current] if current else []) + upcoming
                if not tracks_to_save:
                    return "[SILENT]Nothing to save — queue is empty."
                payload = [_track_to_dict(t) for t in tracks_to_save]
                existed = await self._playlist_repo.load(guild_id_str, name_clean) is not None
                await self._playlist_repo.save(guild_id_str, name_clean, payload)
                verb = "Updated" if existed else "Saved"
                count = len(payload)
                return (
                    f"[SILENT]{verb} playlist '{name_clean}' with {count} track"
                    f"{'s' if count != 1 else ''}."
                )

            if action == "load_playlist":
                stored = await self._playlist_repo.load(guild_id_str, name_clean)
                if stored is None:
                    return f"[SILENT]No playlist named '{name_clean}'."
                tracks = [_track_from_dict(d, user_id, requester_name) for d in stored]
                for track in tracks:
                    player.queue.add(track)
                count = len(tracks)
                return (
                    f"[SILENT]Loaded playlist '{name_clean}' — {count} track"
                    f"{'s' if count != 1 else ''} queued."
                )

            # action == "delete_playlist"
            removed = await self._playlist_repo.delete(guild_id_str, name_clean)
            if not removed:
                return f"[SILENT]No playlist named '{name_clean}'."
            return f"[SILENT]Deleted playlist '{name_clean}'."

        if action == "list_playlists":
            if self._playlist_repo is None:
                return "[SILENT]Playlists aren't configured on this stack yet."
            names = await self._playlist_repo.list_names(str(guild.id))
            if not names:
                return "[SILENT]No saved playlists yet."
            return f"[SILENT]Playlists: {', '.join(names)}."

        # --- Spotify playlist URL import ---
        # Resolves a Spotify public-playlist URL to a list of title+artist
        # dicts, then runs each through AsyncYTDL.search to enqueue. See
        # docs/plans/music-spotify-playlist-import.md.
        if action == "queue_spotify_playlist":
            resolver = self._spotify_resolver
            if resolver is None or not resolver.is_configured():
                return (
                    "[SILENT]Spotify isn't configured. Set "
                    "SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET to use "
                    "playlist URLs."
                )
            url_clean = (url_arg or "").strip()
            if not url_clean:
                return "[SILENT]Spotify playlist URL?"
            tracks_meta = await resolver.resolve(url_clean)
            if tracks_meta is None:
                return "[SILENT]That doesn't look like a Spotify playlist URL."
            if not tracks_meta:
                return "[SILENT]Couldn't find any tracks from that playlist."
            resolved: list[str] = []
            not_found: list[str] = []
            already_playing = player.is_playing
            for meta in tracks_meta:
                title = meta.get("title", "").strip()
                artist = meta.get("artist", "").strip()
                if not title:
                    continue
                query = f"{title} {artist}".strip()
                track = await self._ytdl.search(
                    query, requester_id=user_id, requester_name=requester_name,
                )
                if not track:
                    not_found.append(title)
                    continue
                await player.play(track, deferred=voice)
                resolved.append(track.title)
            if not resolved:
                return "[SILENT]Couldn't find any tracks from that playlist."
            head = (
                f"[SILENT]Queued {len(resolved)} from Spotify"
                if already_playing
                else f"[SILENT]Playing {resolved[0]}, queued {len(resolved) - 1} more from Spotify"
            )
            if not_found:
                head += f" ({len(not_found)} not found)"
            return f"{head}."

        # --- Lyrics fetch ---
        # v1 returns formatted lyrics inline in the [SILENT] reply
        # (truncated to ~1800 chars). Live-overlay tick loop deferred to
        # v2 per docs/plans/music-synced-lyrics.md.
        if action == "lyrics":
            if self._lyrics_resolver is None:
                return "[SILENT]Lyrics aren't configured on this stack yet."
            track = player.current_track
            if track is None:
                return "[SILENT]Nothing is playing — can't show lyrics for nothing."
            # Heuristic title/artist split — YouTube titles often follow
            # "Song - Artist" or "Artist - Song". We try the first form
            # and fall back to the bare title if the split looks wrong.
            parts = [p.strip() for p in track.title.split(" - ", 1)]
            if len(parts) == 2 and parts[0] and parts[1]:
                title_clean, artist_clean = parts[0], parts[1]
            else:
                title_clean, artist_clean = track.title.strip(), ""
            lyrics = await self._lyrics_resolver.fetch(
                title_clean, artist_clean or None,
            )
            if lyrics is None:
                return f"[SILENT]No lyrics found for '{title_clean}'."
            body = _format_lyrics_for_reply(lyrics)
            sync_note = "" if lyrics.is_synced else " (plain — no synced version available)"
            return f"[SILENT]Lyrics for {title_clean}{sync_note}:\n\n{body}"

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
