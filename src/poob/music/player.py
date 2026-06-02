"""Music player — MixingAudioSource and GuildMusicPlayer.

MixingAudioSource is a custom discord.AudioSource that mixes a primary music
stream with a TTS overlay in real-time using numpy. When TTS is active, music
is ducked (volume reduced) with smooth gain ramps to avoid transient clicks.

GuildMusicPlayer owns the queue, voice client, and player loop. One instance
per guild. Uses event-driven transitions (asyncio.Event) — no polling.

Architecture:
    MixingAudioSource.read() called 50x/sec by Pycord's audio thread
    ├── Read 3840 bytes from music FFmpegPCMAudio
    ├── Read 3840 bytes from TTS overlay (if active)
    ├── numpy: duck music if TTS present, sum, clip
    └── Return mixed 3840 bytes → Opus encoder → DAVE → UDP

    GuildMusicPlayer.player_loop() (async task)
    ├── await queue.get_next()
    ├── resolve stream URL (lazy, just-in-time)
    ├── create FFmpegPCMAudio → feed to mixer as primary
    ├── vc.play(mixer) with after callback → event.set()
    ├── pre-fetch next track URL while current plays
    └── await event → loop
"""

from __future__ import annotations

import asyncio
import os
import queue as _queue
import shutil
import glob
import tempfile
import threading
from typing import TYPE_CHECKING

import audioop
import discord

from poob.music.effects import (
    EFFECT_NONE,
    EffectNotFoundError,
    resolve_effect_chain,
)
from poob.music.queue import MusicQueue, Track, LoopMode
from poob.music.ytdl import AsyncYTDL, FFMPEG_BEFORE_OPTS, FFMPEG_OPTS
from poob.utils.logging import get_logger

if TYPE_CHECKING:
    pass

log = get_logger("music.player")

# Discord PCM frame: 20ms of 48kHz stereo 16-bit = 3840 bytes
FRAME_SIZE = 3840

# Ducking parameters — expressed as audioop.mul scale factors (float).
# 0.25 = 25% volume, 1.0 = full volume.
DUCK_VOLUME = 0.25
NORMAL_VOLUME = 1.0
RAMP_FRAMES = 15          # Frames to ramp volume (15 * 20ms = 300ms)
GRACE_FRAMES = 8          # Empty overlay reads before cleanup (160ms grace)

# Before the player loop calls vc.play(mixer), the shared voice client may
# be busy with a standalone TTS clip (e.g. the "Playing X" confirmation
# spoken in VC). vc.play() raises "Already playing audio" if we barge in,
# crashing the loop. Wait for the clip to finish — bounded so a stuck clip
# can't hang music forever. See docs/incidents/already-playing-audio-crash.
CLIENT_FREE_TIMEOUT_S = 8.0   # max wait for a standalone clip to clear
CLIENT_FREE_POLL_S = 0.05     # poll interval while waiting

# Pre-allocated silence buffer (avoids per-frame allocation)
SILENCE = b"\x00" * FRAME_SIZE


def _find_ffmpeg() -> str:
    """Find ffmpeg, checking common Windows install locations."""
    found = shutil.which("ffmpeg")
    if found:
        return found
    winget_pattern = os.path.expanduser(
        "~/AppData/Local/Microsoft/WinGet/Packages/Gyan.FFmpeg*/ffmpeg-*-full_build/bin/ffmpeg.exe"
    )
    matches = glob.glob(winget_pattern)
    if matches:
        return matches[0]
    for candidate in [
        r"C:\ffmpeg\bin\ffmpeg.exe",
        r"C:\ProgramData\chocolatey\bin\ffmpeg.exe",
        r"C:\tools\ffmpeg\bin\ffmpeg.exe",
    ]:
        if os.path.isfile(candidate):
            return candidate
    return "ffmpeg"


FFMPEG_PATH = _find_ffmpeg()


# ---------------------------------------------------------------------------
# MixingAudioSource — the core PCM mixer
# ---------------------------------------------------------------------------


class MixingAudioSource(discord.AudioSource):
    """Real-time PCM mixer that blends music with TTS overlays.

    Pycord's audio thread calls read() every 20ms. We read from both the
    primary source (music) and overlay source (TTS), mix them with audioop,
    and return a single 3840-byte frame.

    Uses audioop (C extension) instead of numpy — zero per-frame allocations,
    built-in int16 clipping, ~5-10x faster than numpy for small buffers.

    When TTS is injected via play_overlay(), music volume ramps down over
    300ms (RAMP_FRAMES). When TTS ends, music ramps back up. This prevents
    the audible click/pop that instant volume changes cause.

    Thread safety: read() runs in Pycord's audio daemon thread. play_overlay()
    and stop_overlay() may be called from the asyncio thread. The overlay
    reference is set atomically (Python's GIL ensures single-pointer writes
    are atomic), and the grace period prevents premature cleanup.
    """

    def __init__(self, primary: discord.AudioSource, volume: float = 0.5) -> None:
        self.primary = primary
        self._base_volume = volume  # User-set music volume (0.0-2.0)
        self._overlay: discord.AudioSource | None = None

        # Gain ramp state — smooths volume transitions
        self._current_duck = NORMAL_VOLUME  # Current duck multiplier (ramping)
        self._target_duck = NORMAL_VOLUME   # Target duck multiplier
        self._ramp_step = 0.0               # Per-frame increment toward target

        # Grace period: don't kill overlay on first empty read (buffering)
        self._empty_overlay_count = 0

    @property
    def volume(self) -> float:
        return self._base_volume

    @volume.setter
    def volume(self, val: float) -> None:
        self._base_volume = max(0.0, min(2.0, val))

    def play_overlay(self, source: discord.AudioSource) -> None:
        """Inject a TTS overlay — music will duck automatically."""
        self._overlay = source
        self._empty_overlay_count = 0
        self._target_duck = DUCK_VOLUME
        self._ramp_step = (self._target_duck - self._current_duck) / max(RAMP_FRAMES, 1)
        log.debug("Overlay injected, ducking music")

    def stop_overlay(self) -> None:
        """Remove the overlay and restore music volume."""
        if self._overlay is not None:
            try:
                self._overlay.cleanup()
            except Exception:
                pass
            self._overlay = None
        self._target_duck = NORMAL_VOLUME
        self._ramp_step = (self._target_duck - self._current_duck) / max(RAMP_FRAMES, 1)
        self._empty_overlay_count = 0
        log.debug("Overlay removed, restoring music volume")

    @property
    def has_overlay(self) -> bool:
        return self._overlay is not None

    def _apply_volume(self, data: bytes) -> bytes:
        """Apply base volume and duck multiplier via audioop.mul.

        audioop.mul(data, width, factor) scales int16 samples with built-in
        clipping — no intermediate arrays, no overflow risk, runs in C.
        """
        factor = self._base_volume * self._current_duck
        if 0.99 < factor < 1.01:
            return data  # Unity gain — zero-cost passthrough
        return audioop.mul(data, 2, factor)

    def read(self) -> bytes:
        """Read and mix one 20ms frame (3840 bytes).

        Called from Pycord's audio daemon thread — must be fast (<20ms).
        Uses audioop C functions: zero allocations, built-in int16 clipping.
        """
        # Read primary (music) frame
        primary_data = self.primary.read()
        if not primary_data:
            return b""

        # Pad short frames with silence
        if len(primary_data) < FRAME_SIZE:
            primary_data += b"\x00" * (FRAME_SIZE - len(primary_data))

        # Update gain ramp
        if self._current_duck != self._target_duck:
            self._current_duck += self._ramp_step
            # Clamp to prevent overshoot
            if self._ramp_step > 0:
                self._current_duck = min(self._current_duck, self._target_duck)
            else:
                self._current_duck = max(self._current_duck, self._target_duck)

        # Fast path: no overlay — just apply volume and duck
        if self._overlay is None:
            return self._apply_volume(primary_data)

        # Read overlay (TTS) frame
        overlay_data = self._overlay.read()

        if not overlay_data or len(overlay_data) < FRAME_SIZE:
            self._empty_overlay_count += 1
            if self._empty_overlay_count >= GRACE_FRAMES:
                self.stop_overlay()
            return self._apply_volume(primary_data)

        # Reset empty counter — overlay is producing audio
        self._empty_overlay_count = 0

        # Pad overlay if short
        if len(overlay_data) < FRAME_SIZE:
            overlay_data += b"\x00" * (FRAME_SIZE - len(overlay_data))

        # Mix: duck music, add TTS at full volume, clip (all in C)
        music_scaled = self._apply_volume(primary_data)
        return audioop.add(music_scaled, overlay_data, 2)

    def is_opus(self) -> bool:
        return False  # We output raw PCM — Pycord handles Opus encoding

    def cleanup(self) -> None:
        """Release all audio resources."""
        try:
            self.primary.cleanup()
        except Exception:
            pass
        if self._overlay is not None:
            try:
                self._overlay.cleanup()
            except Exception:
                pass
            self._overlay = None


# ---------------------------------------------------------------------------
# BufferedAudioSource — read-ahead buffer to absorb FFmpeg/network jitter
# ---------------------------------------------------------------------------


class BufferedAudioSource(discord.AudioSource):
    """Wraps an AudioSource with a read-ahead buffer backed by a reader thread.

    Pycord's audio thread calls read() every 20ms on the main audio thread.
    If the underlying source blocks (FFmpeg pipe waiting on network, CDN
    reconnect, TLS renegotiation), that frame is late and causes stuttering.

    This wrapper decouples I/O from the audio thread:
    - A dedicated daemon thread reads from the underlying source as fast as
      FFmpeg can produce data, filling a thread-safe queue.
    - The audio thread reads from the queue — always fast, never blocks
      on network I/O.
    - On queue underrun, returns silence instead of empty bytes. Empty bytes
      signal end-of-stream to Pycord's AudioPlayer (which would stop
      playback). Silence keeps the stream alive while the buffer refills.

    Args:
        source: The underlying AudioSource (typically FFmpegPCMAudio).
        buffer_frames: Max queue depth in frames. 100 = 2 seconds of buffer.
        prefill: Frames to buffer before read() returns real data. Ensures
            the queue has a head start before the audio thread starts pulling.
    """

    def __init__(
        self,
        source: discord.AudioSource,
        buffer_frames: int = 100,
        prefill: int = 10,
    ) -> None:
        self._source = source
        self._queue: _queue.Queue[bytes | None] = _queue.Queue(maxsize=buffer_frames)
        self._end = threading.Event()
        self._prefilled = threading.Event()
        self._prefill_count = prefill
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        # Block until prefill frames are queued (max 5s safety timeout)
        self._prefilled.wait(timeout=5.0)

    def _read_loop(self) -> None:
        """Producer thread: reads from FFmpeg pipe into the queue."""
        count = 0
        while not self._end.is_set():
            data = self._source.read()
            if not data:
                self._queue.put(None)  # EOF sentinel
                break
            try:
                self._queue.put(data, timeout=1.0)
            except _queue.Full:
                pass  # Backpressure — queue is full, reader waits
            count += 1
            if count == self._prefill_count:
                self._prefilled.set()
        # If we never hit prefill count (very short track), unblock anyway
        self._prefilled.set()

    def read(self) -> bytes:
        """Consumer: called by Pycord's audio thread every 20ms."""
        try:
            data = self._queue.get(timeout=0.02)
        except _queue.Empty:
            return SILENCE  # Underrun — return silence, don't stop playback
        if data is None:
            return b""  # True EOF — track is over
        return data

    def is_opus(self) -> bool:
        return False

    def cleanup(self) -> None:
        """Stop the reader thread and release the underlying source."""
        self._end.set()
        try:
            self._source.cleanup()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# GuildMusicPlayer — per-guild player with queue and event-driven transitions
# ---------------------------------------------------------------------------


class GuildMusicPlayer:
    """Music player for a single Discord guild.

    Owns the MusicQueue, manages the MixingAudioSource lifecycle, and runs
    an async player_loop that handles track transitions via asyncio.Event
    (not polling). Pre-fetches the next track's stream URL during playback
    for near-gapless transitions.

    Args:
        voice_client: Connected Pycord VoiceClient.
        ytdl: Shared AsyncYTDL instance.
        volume: Default music volume (0.0-1.0).
        idle_timeout: Seconds of idle before auto-cleanup (0 = disabled).
    """

    def __init__(
        self,
        voice_client: discord.VoiceClient,
        ytdl: AsyncYTDL,
        volume: float = 0.5,
        idle_timeout: float = 300.0,
    ) -> None:
        self.voice_client = voice_client
        self.ytdl = ytdl
        self.queue = MusicQueue()
        self._volume = volume
        self._idle_timeout = idle_timeout

        self._mixer: MixingAudioSource | None = None
        self._player_task: asyncio.Task | None = None
        self._next_event = asyncio.Event()
        self._loop = asyncio.get_event_loop()
        self._destroyed = False
        self._paused = False
        self._skip_requested = False

        # Pre-fetch state
        self._prefetch_task: asyncio.Task | None = None

        # ---- Position tracking (wall-clock, pause-aware) ----
        # Set when ``voice_client.play`` is called for the live source.
        # ``_paused_at`` is non-None only while paused. ``_total_pause_seconds``
        # accumulates closed pause intervals. See ``position_seconds`` for
        # the formula. Cleared at each respawn (a respawn defines a new t=0
        # at the seek offset, so wall-clock resets to the seek point).
        self._track_started_at: float | None = None
        self._paused_at: float | None = None
        self._total_pause_seconds: float = 0.0
        # Offset baked into the current FFmpeg invocation via ``-ss``.
        # Added to wall-clock delta so ``position_seconds`` reports the
        # listener-perceived track position, not the source-process uptime.
        self._track_seek_offset: float = 0.0

        # ---- Effect state ----
        # ``_active_effect`` is the user-facing name (``"none"``,
        # ``"nightcore"``, ...). ``_active_effect_chain`` is the resolved
        # FFmpeg ``-af`` string or ``None`` for the no-filter path. They
        # are kept in sync via ``set_effect``.
        self._active_effect: str = EFFECT_NONE
        self._active_effect_chain: str | None = None

        # ---- Respawn request ----
        # When set, ``_player_loop`` rebuilds the audio source instead of
        # advancing the queue. Tuple of (track, seek_position, effect_chain).
        # See ``set_effect`` / ``replay`` / ``previous`` for the producers.
        self._respawn_request: tuple[Track, float, str | None] | None = None

        # ---- Autoplay state ----
        # When ``autoplay_enabled`` is True and the queue empties, the
        # player loop consults ``_get_autoplay_engine()`` to generate a
        # next track from ``_last_played_track``. See
        # docs/plans/music-autoplay.md for the cascade and rationale.
        # Per-guild runtime state — resets on bot restart by design.
        self.autoplay_enabled: bool = False
        self._last_played_track: Track | None = None
        self._autoplay_engine: "AutoplayEngine | None" = None

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def is_playing(self) -> bool:
        return self.voice_client.is_playing() and not self._paused

    @property
    def is_paused(self) -> bool:
        return self._paused

    @property
    def current_track(self) -> Track | None:
        return self.queue.current

    async def _await_voice_client_free(self) -> None:
        """Wait for the shared voice client to stop playing a standalone clip
        before the loop calls ``vc.play(mixer)``.

        The voice session plays one-off TTS (a "Playing X" confirmation, a
        spoken chat reply) directly on the same voice client. Starting music
        while one is active raises Pycord's ``ClientException: Already playing
        audio`` and crashes the player loop, so the requested song never plays.
        The session itself waits the same way before its TTS.

        Bounded by ``CLIENT_FREE_TIMEOUT_S`` so a stuck clip can't block music
        forever — on timeout, stop the lingering source (the explicit play
        request takes precedence) and return. See
        docs/incidents/already-playing-audio-crash.
        """
        import time as _t
        deadline = _t.monotonic() + CLIENT_FREE_TIMEOUT_S
        while self.voice_client.is_playing() and not self._destroyed:
            if _t.monotonic() >= deadline:
                try:
                    self.voice_client.stop()
                except Exception:
                    pass
                return
            await asyncio.sleep(CLIENT_FREE_POLL_S)

    # ------------------------------------------------------------------
    # Autoplay
    # ------------------------------------------------------------------

    def _get_autoplay_engine(self) -> "AutoplayEngine":
        """Lazy-construct the autoplay engine so import / API setup cost
        only lands the first time autoplay actually fires."""
        if self._autoplay_engine is None:
            from poob.music.autoplay import AutoplayEngine
            self._autoplay_engine = AutoplayEngine(
                ytdl=self.ytdl,
                history_accessor=lambda: self.queue.history,
            )
        return self._autoplay_engine

    async def _try_autoplay_inject(self) -> Track | None:
        """When the queue empties and autoplay is on, generate the next
        track via the cascade and enqueue it. Returns the new
        ``queue.current`` (or ``None`` if autoplay is off / has nothing
        to add). Never raises. See docs/plans/music-autoplay.md."""
        if not self.autoplay_enabled:
            return None
        seed = self._last_played_track
        if seed is None:
            return None
        try:
            generated = await self._get_autoplay_engine().get_next(seed)
        except Exception as exc:
            log.warning("autoplay engine raised", error=str(exc)[:120])
            return None
        if generated is None:
            return None
        log.info("autoplay enqueued", title=generated.title[:60])
        self.queue.add(generated)
        return self.queue.get_next()

    @property
    def mixer(self) -> MixingAudioSource | None:
        return self._mixer

    @property
    def volume(self) -> float:
        return self._volume

    @volume.setter
    def volume(self, val: float) -> None:
        self._volume = max(0.0, min(2.0, val))
        if self._mixer:
            self._mixer.volume = self._volume

    @property
    def active_effect(self) -> str:
        """The currently-active effect name (``"none"`` or a preset name)."""
        return self._active_effect

    @property
    def position_seconds(self) -> float:
        """Listener-perceived position of the current track, in seconds.

        Returns 0.0 when nothing is playing. Otherwise: wall-clock since
        the FFmpeg process started, minus total paused intervals, plus
        any ``-ss`` seek offset baked into the current invocation.
        Includes the in-flight pause interval if currently paused, so the
        position freezes during a pause instead of drifting forward.

        Used by ``set_effect`` to seek the respawned source to the same
        spot the listener was at — the cost is a ~200-400 ms gap, not a
        forward jump in the music.
        """
        import time as _t
        if self._track_started_at is None:
            return 0.0
        now = _t.monotonic()
        wall = now - self._track_started_at
        paused = self._total_pause_seconds
        if self._paused_at is not None:
            paused += now - self._paused_at
        return max(0.0, wall - paused + self._track_seek_offset)

    # ------------------------------------------------------------------
    # Playback control
    # ------------------------------------------------------------------

    async def play(self, track: Track, *, deferred: bool = False) -> None:
        """Add a track and start playing if not already.

        Args:
            track: Track to add.
            deferred: If True, queue the track but don't start playback.
                Call start_deferred() later to begin. Used for voice mode
                where Poob speaks about the song before it starts.
        """
        self.queue.add(track)
        if deferred:
            # Pre-resolve the stream URL in background so it's ready
            self._loop.create_task(self._pre_resolve(track))
            return
        if self._player_task is None or self._player_task.done():
            self._player_task = self._loop.create_task(self._player_loop())

    async def _pre_resolve(self, track: Track) -> None:
        """Pre-download a deferred track so it's ready when playback starts."""
        try:
            result = await self.ytdl.download_track(track)
            if result:
                log.info("Pre-downloaded deferred track", title=track.title[:50])
            else:
                # Fallback: resolve stream URL for streaming playback
                await self.ytdl.resolve_stream_url(track)
                log.info("Pre-resolved deferred track (stream)", title=track.title[:50])
        except Exception:
            pass  # Will retry at play time

    def start_deferred(self) -> None:
        """Start playback of previously deferred tracks.

        Called after Poob finishes speaking about the song request.
        """
        if self._player_task is None or self._player_task.done():
            self._player_task = self._loop.create_task(self._player_loop())
            log.info("Deferred playback started")

    async def play_now(self, track: Track) -> None:
        """Insert track at front and skip current."""
        self.queue.add_next(track)
        await self.skip()

    async def play_many(self, tracks: list[Track], *, deferred: bool = False) -> int:
        """Add multiple tracks. Starts playing if idle. Returns count added."""
        count = self.queue.add_many(tracks)
        if not deferred and (self._player_task is None or self._player_task.done()):
            self._player_task = self._loop.create_task(self._player_loop())
        return count

    async def skip(self) -> Track | None:
        """Skip the current track."""
        self._skip_requested = True
        if self.voice_client.is_playing() or self._paused:
            self.voice_client.stop()  # Triggers after callback → event.set()
        return self.queue.current

    async def replay(self) -> Track | None:
        """Restart the current track from the beginning.

        Implemented via the respawn mechanism: keeps current set, asks
        the loop to rebuild the audio source at position 0 with the
        same active effect chain. Audible cost is a ~200-400 ms gap
        while FFmpeg respawns — see
        ``docs/gotchas/ffmpeg-effect-toggle-creates-audio-gap.md``.
        Returns the track being replayed, or ``None`` if nothing is
        currently playing.
        """
        cur = self.queue.current
        if cur is None:
            return None
        self._respawn_request = (cur, 0.0, self._active_effect_chain)
        if self.voice_client.is_playing() or self._paused:
            self.voice_client.stop()  # Triggers after callback → respawn
        return cur

    async def previous(self) -> Track | None:
        """Play the most-recently finished track again.

        Swaps the queue state so the previous track plays now and the
        currently-playing one is inserted at the queue front (it will
        play after the previous finishes). The previous track itself is
        popped from history. Returns the previous track, or ``None`` if
        history is empty.

        Pops + swap happen atomically here (under the asyncio single-
        threaded model) so a rapid double-press walks back two steps,
        not zero or one.
        """
        prev = self.queue.previous()
        if prev is None:
            return None
        if self.queue.current is not None:
            self.queue.add_next(self.queue.current)
        self.queue.current = prev
        self._respawn_request = (prev, 0.0, self._active_effect_chain)
        if self.voice_client.is_playing() or self._paused:
            self.voice_client.stop()
        return prev

    async def seek(self, parsed) -> tuple[Track, float] | None:  # type: ignore[no-untyped-def]
        """Seek to an absolute or relative position within the current track.

        ``parsed`` is a ``poob.music.seek.ParsedSeek`` instance — the
        caller has already normalized the user input. Relative offsets
        add to ``position_seconds``; absolute offsets become the new
        listener position. Negative results clamp to 0; values past the
        track duration clamp to ``duration - 1.0`` so the seek doesn't
        immediately end-of-stream.

        Returns ``(track, target_seconds)`` on success, or ``None`` if
        there's no current track to seek within. Reuses the respawn
        mechanism (see [[music-on-the-fly-filter-respawn]]) — so the
        same ~200-400 ms audible gap applies.

        Live streams have no duration to clamp against; seek attempts
        on a stream return ``None`` and the caller surfaces an error
        rather than trying to spawn ``-ss`` on something that doesn't
        support it.
        """
        cur = self.queue.current
        if cur is None:
            return None
        if cur.is_stream:
            return None

        if parsed.relative:
            target = self.position_seconds + parsed.seconds
        else:
            target = parsed.seconds

        # Clamp below 0 and above duration. Duration may be None for
        # rare edge cases (yt-dlp returning no duration on a non-stream);
        # in that case skip the upper clamp.
        target = max(0.0, target)
        if cur.duration is not None:
            duration_seconds = cur.duration.total_seconds()
            if duration_seconds > 0:
                # Leave 1s of headroom — seeking past the end is the
                # same as "skip", which is a separate action.
                target = min(target, duration_seconds - 1.0)
                target = max(0.0, target)

        self._respawn_request = (cur, target, self._active_effect_chain)
        if self.voice_client.is_playing() or self._paused:
            self.voice_client.stop()
        return cur, target

    async def set_effect(self, effect: str) -> str | None:
        """Apply (or clear) an audio effect on the current track.

        ``effect`` is one of the names in ``poob.music.effects.
        AVAILABLE_EFFECTS`` (``"none"`` to clear). The effect is also
        remembered as the default for subsequent tracks until changed —
        so applying ``"nightcore"`` mid-song and then skipping forward
        keeps the nightcore on the next track too.

        Returns the applied effect name on success, or ``None`` if
        there's no current track to apply it to (the effect is still
        stored as the default for the next track in that case). Raises
        ``EffectNotFoundError`` for unknown effect names — caller's
        responsibility to validate before invoking from a user-facing
        path.

        Implementation: respawns the FFmpeg subprocess with the new
        ``-af`` chain and an ``-ss`` seek to the current listener
        position. ~200-400 ms audible gap during the respawn.
        """
        # resolve_effect_chain raises EffectNotFoundError on unknown
        # names — propagate; the caller wraps for the user.
        chain = resolve_effect_chain(effect)
        # Normalize the stored effect name to lowercase (matches the
        # registry's canonical form).
        self._active_effect = effect.strip().lower()
        self._active_effect_chain = chain

        cur = self.queue.current
        if cur is None:
            # No current track — effect stored for the next play.
            return None

        pos = self.position_seconds
        self._respawn_request = (cur, pos, chain)
        if self.voice_client.is_playing() or self._paused:
            self.voice_client.stop()
        return self._active_effect

    def pause(self) -> bool:
        """Pause playback. Returns True if paused.

        Records the start of the pause interval so ``position_seconds``
        can subtract it when computing seek offsets for respawns.
        """
        import time as _t
        if self.voice_client.is_playing():
            self.voice_client.pause()
            self._paused = True
            self._paused_at = _t.monotonic()
            return True
        return False

    def resume(self) -> bool:
        """Resume playback. Returns True if resumed.

        Closes the in-flight pause interval and rolls it into the
        accumulator. ``position_seconds`` reads from both, so a track
        paused for an hour then resumed reports the same position it
        had at pause time.
        """
        import time as _t
        if self._paused:
            self.voice_client.resume()
            self._paused = False
            if self._paused_at is not None:
                self._total_pause_seconds += _t.monotonic() - self._paused_at
                self._paused_at = None
            return True
        return False

    def toggle_pause(self) -> bool:
        """Toggle pause/resume. Returns True if now paused."""
        if self._paused:
            self.resume()
            return False
        else:
            self.pause()
            return True

    async def stop(self) -> None:
        """Stop playback and clear the queue.

        Disables autoplay too: without this, "stop" clears the queue and
        halts the current track, but the player loop wakes, finds the queue
        empty, and (autoplay still on) immediately injects a fresh track —
        so "stop" never actually stops. Disabling here is the right layer:
        it covers every stop path (text "stop", the now-playing button,
        and "leave"). See docs/incidents/stop-does-not-disable-autoplay.md.
        """
        self.autoplay_enabled = False
        # Clean up temp files for current and queued tracks
        if self.queue.current:
            AsyncYTDL.cleanup_track_file(self.queue.current)
        for track in self.queue.upcoming:
            AsyncYTDL.cleanup_track_file(track)
        self.queue.clear_all()
        self._skip_requested = False
        if self.voice_client.is_playing() or self._paused:
            self._paused = False
            self.voice_client.stop()
        if self._mixer:
            self._mixer.cleanup()
            self._mixer = None

    # ------------------------------------------------------------------
    # TTS overlay (called by VoiceSession)
    # ------------------------------------------------------------------

    def inject_tts_overlay(self, tts_source: discord.AudioSource) -> bool:
        """Inject a TTS audio source as an overlay on the music.

        Returns True if injected, False if no mixer is active.
        Called from VoiceSession._play_audio() when music is playing.
        """
        if self._mixer is not None:
            self._mixer.play_overlay(tts_source)
            return True
        return False

    def has_active_overlay(self) -> bool:
        """Check if TTS overlay is currently playing."""
        return self._mixer is not None and self._mixer.has_overlay

    async def wait_overlay_done(self, timeout: float = 30.0) -> None:
        """Wait for the TTS overlay to finish (with timeout)."""
        elapsed = 0.0
        while self.has_active_overlay() and elapsed < timeout:
            await asyncio.sleep(0.05)
            elapsed += 0.05

    # ------------------------------------------------------------------
    # Player loop (event-driven, no polling)
    # ------------------------------------------------------------------

    def _make_audio_source(
        self,
        track: Track,
        *,
        seek_seconds: float = 0.0,
        effect_chain: str | None = None,
    ) -> discord.AudioSource:
        """Create the audio source chain for a track.

        For local files (pre-downloaded): FFmpeg reads from disk — minimal
        FFmpeg flags needed, no network reconnect logic.

        For stream URLs (livestreams, download failures): FFmpeg reads from
        network with full reconnect flags and read-ahead buffer.

        Both paths wrap FFmpegPCMAudio in BufferedAudioSource to decouple
        FFmpeg's pipe I/O from Pycord's 20ms audio thread timing.

        ``seek_seconds`` adds ``-ss <pos>`` to ``before_options`` for
        respawn-resume. ``effect_chain`` adds ``-af <chain>`` to
        ``options`` for filter presets. Both are no-op when their
        argument is the default — no extra flag emitted.
        """
        # -ss before input is the fast seek path (keyframe-aligned).
        # Append to the existing before_options string when non-zero.
        seek_flag = f" -ss {seek_seconds:.3f}" if seek_seconds > 0 else ""
        # -af after input adds the filter chain. Append to options.
        af_flag = f" -af \"{effect_chain}\"" if effect_chain else ""

        if track.local_file and os.path.isfile(track.local_file):
            ffmpeg_source = discord.FFmpegPCMAudio(
                track.local_file,
                executable=FFMPEG_PATH,
                before_options=f"-nostdin{seek_flag}",
                options=f"{FFMPEG_OPTS}{af_flag}",
            )
            log.debug(
                "Audio source: local file",
                file=track.local_file[-40:],
                seek=seek_seconds, effect=bool(effect_chain),
            )
        else:
            ffmpeg_source = discord.FFmpegPCMAudio(
                track.stream_url,
                executable=FFMPEG_PATH,
                before_options=f"{FFMPEG_BEFORE_OPTS}{seek_flag}",
                options=f"{FFMPEG_OPTS}{af_flag}",
            )
            log.debug(
                "Audio source: network stream",
                seek=seek_seconds, effect=bool(effect_chain),
            )

        return BufferedAudioSource(ffmpeg_source)

    async def _player_loop(self) -> None:
        """Main playback loop — runs as a background task.

        Flow:
        1. Get next track from queue
        2. Pre-download to temp file (or resolve stream URL for livestreams)
        3. FFmpegPCMAudio → BufferedAudioSource → MixingAudioSource → vc.play()
        4. Pre-download next track in background
        5. Wait for after callback (track finished / skipped)
        6. Clean up temp file
        7. Loop
        """
        log.info("Player loop started")
        try:
            while not self._destroyed:
                self._next_event.clear()
                self._skip_requested = False

                # Get next track
                track = self.queue.get_next() if self.queue.current is None else self.queue.current
                if track is None:
                    track = self.queue.get_next()
                if track is None:
                    track = await self._try_autoplay_inject()
                if track is None:
                    log.info("Queue empty, player loop ending")
                    break

                # Remember the last track that actually reaches the
                # play stage — the autoplay engine seeds the next round
                # off this. We update here rather than after the FFmpeg
                # respawn loop so a respawn (replay / set_effect /
                # previous) doesn't blank the seed back to None.
                self._last_played_track = track

                # Acquire audio: pre-download to local file, fall back to stream URL
                if not track.local_file and not track.stream_url:
                    log.info("Downloading track", title=track.title[:60])
                    local = await self.ytdl.download_track(track)
                    if not local:
                        # Download failed — fall back to stream URL
                        log.info("Download failed, resolving stream URL", title=track.title[:60])
                        url = await self.ytdl.resolve_stream_url(track)
                        if not url:
                            log.warning("Failed to get audio, skipping", title=track.title[:60])
                            self.queue.current = None
                            continue

                log.info(
                    "Playing track",
                    title=track.title[:60],
                    duration=track.duration_str,
                    local=bool(track.local_file),
                )

                # Inner loop: rebuild the audio source in place when a
                # respawn is requested (set_effect / replay / previous).
                # Without this, those operations would have to advance
                # the queue via get_next, which double-pushes history
                # and breaks history-walker semantics.
                seek_seconds = 0.0
                effect_chain = self._active_effect_chain
                respawning = False  # True after at least one respawn this track
                while not self._destroyed:
                    try:
                        buffered = self._make_audio_source(
                            track,
                            seek_seconds=seek_seconds,
                            effect_chain=effect_chain,
                        )
                    except Exception as exc:
                        log.error(
                            "Audio source creation failed",
                            error=str(exc)[:120], respawn=respawning,
                        )
                        if not respawning:
                            # Initial source failed — abandon track, advance
                            AsyncYTDL.cleanup_track_file(track)
                            self.queue.current = None
                        break

                    self._mixer = MixingAudioSource(buffered, volume=self._volume)
                    self._paused = False
                    self._paused_at = None
                    self._total_pause_seconds = 0.0
                    self._track_seek_offset = seek_seconds

                    def _after_play(error: Exception | None) -> None:
                        if error:
                            log.warning("Playback error", error=str(error)[:100])
                        self._loop.call_soon_threadsafe(self._next_event.set)

                    # The voice session may be speaking a standalone TTS clip
                    # on this same voice client. Wait for it to clear, else
                    # vc.play() raises "Already playing audio" and crashes the
                    # loop. See docs/incidents/already-playing-audio-crash.
                    await self._await_voice_client_free()
                    self._next_event.clear()
                    self.voice_client.play(self._mixer, after=_after_play)
                    import time as _t
                    self._track_started_at = _t.monotonic()

                    # Only prefetch on the FIRST source for this track —
                    # respawns are mid-track and shouldn't re-trigger the
                    # next-track download.
                    if not respawning:
                        self._start_prefetch()

                    await self._next_event.wait()

                    # Cleanup the just-stopped mixer; the FFmpeg subprocess
                    # tied to it dies with cleanup().
                    if self._mixer:
                        try:
                            self._mixer.cleanup()
                        except Exception:
                            pass
                        self._mixer = None

                    # Respawn requested? Pull the new (track, position,
                    # effect) and loop back to rebuild without advancing.
                    if self._respawn_request is not None:
                        new_track, seek_seconds, effect_chain = self._respawn_request
                        self._respawn_request = None
                        # set_effect / replay / previous already updated
                        # queue.current to the right track; sync the loop
                        # variable so cleanup at the end of this iteration
                        # targets the correct track.
                        track = new_track
                        respawning = True
                        continue

                    break  # Natural end or skip — fall through to advance

                AsyncYTDL.cleanup_track_file(track)

                # Advance queue
                if self._skip_requested:
                    self.queue.current = None
                    self._skip_requested = False
                else:
                    self.queue.current = None

                # Reset position-tracking state at the boundary between tracks.
                self._track_started_at = None
                self._track_seek_offset = 0.0
                self._total_pause_seconds = 0.0
                self._paused_at = None

        except asyncio.CancelledError:
            log.info("Player loop cancelled")
        except Exception as exc:
            log.error("Player loop crashed", error=str(exc)[:150])
        finally:
            self._mixer = None
            log.info("Player loop ended")

    def _start_prefetch(self) -> None:
        """Pre-download the next track during current playback.

        Downloads the next queued track to a local temp file so it's ready
        for instant playback when the current track ends. Falls back to
        stream URL resolution if download fails.
        """
        if self._prefetch_task and not self._prefetch_task.done():
            return

        upcoming = self.queue.upcoming
        if not upcoming:
            return

        next_track = upcoming[0]
        if next_track.local_file or next_track.stream_url:
            return  # Already ready

        async def _prefetch():
            try:
                result = await self.ytdl.download_track(next_track)
                if result:
                    log.debug("Pre-downloaded next track", title=next_track.title[:40])
                else:
                    # Fallback: at least have a stream URL ready
                    await self.ytdl.resolve_stream_url(next_track)
                    log.debug("Pre-fetched next track URL", title=next_track.title[:40])
            except Exception:
                pass  # Non-critical — will resolve at play time

        self._prefetch_task = self._loop.create_task(_prefetch())

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def destroy(self) -> None:
        """Full teardown — cancel tasks, clear state, release resources."""
        self._destroyed = True

        if self._player_task and not self._player_task.done():
            self._player_task.cancel()
            try:
                await self._player_task
            except (asyncio.CancelledError, Exception):
                pass

        if self._prefetch_task and not self._prefetch_task.done():
            self._prefetch_task.cancel()

        if self.voice_client.is_playing() or self._paused:
            self.voice_client.stop()

        if self._mixer:
            self._mixer.cleanup()
            self._mixer = None

        # Clean up all temp files
        if self.queue.current:
            AsyncYTDL.cleanup_track_file(self.queue.current)
        for track in self.queue.upcoming:
            AsyncYTDL.cleanup_track_file(track)

        self.queue.clear_all()
        log.info("GuildMusicPlayer destroyed")
