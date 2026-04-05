"""Real-time audio sink for Pycord's native recording pipeline.

Replaces discord-ext-voice-recv's AudioSink with a Pycord-native Sink subclass
that streams decoded PCM audio per-user into our VAD → STT → LLM → TTS pipeline
in real-time (per-packet, not batched).

Pycord calls Sink.write(data, user) for every decoded 20ms opus frame.
We override write() to feed each frame into per-user audio buffers with
voice activity detection, bypassing the default BytesIO accumulation.

Critical: also runs a background thread to flush stale buffers. When a user
stops talking, Discord stops sending packets entirely — no silence frames arrive.
Without the stale-buffer check, the VAD gets stuck in SPEAKING state forever
until a new sound triggers a frame.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from discord.sinks import Sink

logger = logging.getLogger(__name__)

# Discord audio constants
DISCORD_SAMPLE_RATE = 48000
DISCORD_CHANNELS = 2
DISCORD_SAMPLE_WIDTH = 2  # 16-bit
DISCORD_FRAME_MS = 20
DISCORD_FRAME_BYTES = (
    DISCORD_SAMPLE_RATE * DISCORD_CHANNELS * DISCORD_SAMPLE_WIDTH * DISCORD_FRAME_MS // 1000
)  # 3840

# How often to check for stale buffers (ms)
STALE_CHECK_INTERVAL_MS = 100


class RealtimeAudioSink(Sink):
    """Pycord Sink that streams decoded PCM to per-user audio buffers in real-time.

    Also runs a lightweight background thread that checks for stale audio buffers
    every 100ms. This is critical because Discord stops sending packets when a user
    goes silent — without this check, the VAD never detects end-of-speech.

    Args:
        on_audio_frame: Callback ``(user_id: int, pcm_data: bytes) -> None``
            called for every decoded 20ms PCM frame from any user.
        get_buffers: Callback ``() -> dict[int, UserAudioBuffer]`` to access
            the session's per-user audio buffers for stale-flush checks.
    """

    def __init__(
        self,
        on_audio_frame: Callable[[int, bytes], None],
        get_buffers: Callable[[], dict] | None = None,
        on_stale_check: Callable[[], None] | None = None,
    ) -> None:
        super().__init__()
        self._on_audio_frame = on_audio_frame
        self._get_buffers = get_buffers
        self._on_stale_check = on_stale_check  # For dual pipeline stale detection
        self._stopped = threading.Event()
        self._stale_thread: threading.Thread | None = None

        # Start the stale-buffer checker thread
        if get_buffers is not None or on_stale_check is not None:
            self._stale_thread = threading.Thread(
                target=self._stale_buffer_checker,
                daemon=True,
                name="stale-buffer-checker",
            )
            self._stale_thread.start()

    def _stale_buffer_checker(self) -> None:
        """Background thread: flush buffers that haven't received frames recently.

        Runs every 100ms. If a buffer is in SPEAKING state and hasn't received
        a frame for longer than the silence threshold, it means Discord stopped
        sending packets (user went silent). We force-emit the utterance.
        """
        while not self._stopped.is_set():
            self._stopped.wait(STALE_CHECK_INTERVAL_MS / 1000.0)
            if self._stopped.is_set():
                break
            try:
                self._check_stale_buffers()
            except Exception:
                logger.exception("Error in stale buffer check")

    def _check_stale_buffers(self) -> None:
        """Check all user buffers for stale speech that needs flushing."""
        # Dual pipeline has its own stale check
        if self._on_stale_check is not None:
            try:
                self._on_stale_check()
            except Exception:
                logger.exception("Error in dual pipeline stale check")
            return

        if self._get_buffers is None:
            return

        buffers = self._get_buffers()
        now = time.monotonic()

        for buffer in list(buffers.values()):
            # Only check buffers that are actively accumulating speech
            if not hasattr(buffer, "_state") or not hasattr(buffer, "_last_frame_time"):
                continue

            from agentic_scraper.voice.audio_buffer import VADState

            if buffer._state != VADState.SPEAKING:
                continue
            if buffer._last_frame_time <= 0:
                continue

            gap_ms = (now - buffer._last_frame_time) * 1000
            silence_threshold = getattr(buffer.config, "silence_duration_ms", 500)

            if gap_ms > silence_threshold:
                logger.debug(
                    "Flushing stale buffer (no packets for %.0fms)",
                    gap_ms,
                    extra={"user": buffer.user_id},
                )
                buffer._emit_utterance()

    def write(self, data: Any, user: int) -> None:
        """Called by Pycord's recording thread for each decoded opus frame.

        Args:
            data: Raw PCM bytes (48kHz, stereo, 16-bit LE) — one 20ms frame.
            user: Discord user ID (int) of the speaker.
        """
        pcm_bytes = data if isinstance(data, bytes) else bytes(data)

        if not pcm_bytes:
            return

        try:
            self._on_audio_frame(int(user), pcm_bytes)
        except Exception:
            logger.exception("Error in audio frame callback for user %s", user)

    def cleanup(self) -> None:
        """Called when recording stops."""
        self._stopped.set()
        if self._stale_thread is not None:
            self._stale_thread.join(timeout=1.0)
        logger.debug("RealtimeAudioSink cleanup")

    @staticmethod
    def wants_opus() -> bool:
        """We want decoded PCM, not raw opus packets."""
        return False
