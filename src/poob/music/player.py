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

    def pause(self) -> bool:
        """Pause playback. Returns True if paused."""
        if self.voice_client.is_playing():
            self.voice_client.pause()
            self._paused = True
            return True
        return False

    def resume(self) -> bool:
        """Resume playback. Returns True if resumed."""
        if self._paused:
            self.voice_client.resume()
            self._paused = False
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
        """Stop playback and clear the queue."""
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

    def _make_audio_source(self, track: Track) -> discord.AudioSource:
        """Create the audio source chain for a track.

        For local files (pre-downloaded): FFmpeg reads from disk — minimal
        FFmpeg flags needed, no network reconnect logic.

        For stream URLs (livestreams, download failures): FFmpeg reads from
        network with full reconnect flags and read-ahead buffer.

        Both paths wrap FFmpegPCMAudio in BufferedAudioSource to decouple
        FFmpeg's pipe I/O from Pycord's 20ms audio thread timing.
        """
        if track.local_file and os.path.isfile(track.local_file):
            # Local file — no network, minimal FFmpeg config
            ffmpeg_source = discord.FFmpegPCMAudio(
                track.local_file,
                executable=FFMPEG_PATH,
                before_options="-nostdin",
                options=FFMPEG_OPTS,
            )
            log.debug("Audio source: local file", file=track.local_file[-40:])
        else:
            # Network stream — full reconnect + buffer config
            ffmpeg_source = discord.FFmpegPCMAudio(
                track.stream_url,
                executable=FFMPEG_PATH,
                before_options=FFMPEG_BEFORE_OPTS,
                options=FFMPEG_OPTS,
            )
            log.debug("Audio source: network stream")

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
                    log.info("Queue empty, player loop ending")
                    break

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

                # Create audio source chain: FFmpeg → Buffer → Mixer
                try:
                    buffered = self._make_audio_source(track)
                except Exception as exc:
                    log.error("Audio source creation failed", error=str(exc)[:120])
                    AsyncYTDL.cleanup_track_file(track)
                    self.queue.current = None
                    continue

                self._mixer = MixingAudioSource(buffered, volume=self._volume)
                self._paused = False

                # Play with after callback that signals event
                def _after_play(error: Exception | None) -> None:
                    if error:
                        log.warning("Playback error", error=str(error)[:100])
                    self._loop.call_soon_threadsafe(self._next_event.set)

                self.voice_client.play(self._mixer, after=_after_play)

                # Pre-download next track while this one plays
                self._start_prefetch()

                # Wait for track to finish (or skip)
                await self._next_event.wait()

                # Cleanup current mixer and temp file
                if self._mixer:
                    try:
                        self._mixer.cleanup()
                    except Exception:
                        pass
                    self._mixer = None
                AsyncYTDL.cleanup_track_file(track)

                # Advance queue
                if self._skip_requested:
                    self.queue.current = None
                    self._skip_requested = False
                else:
                    self.queue.current = None

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
