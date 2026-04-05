"""Per-user audio buffering with energy-based voice activity detection.

Collects PCM frames from Discord voice, detects speech start/stop
via RMS energy, and emits complete utterances for STT processing.
"""

from __future__ import annotations

import struct
import math
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Callable

from agentic_scraper.utils.logging import get_logger

log = get_logger("voice.buffer")

# Discord sends 20ms frames of 48kHz 16-bit stereo PCM = 3840 bytes/frame
FRAME_DURATION_MS = 20
BYTES_PER_FRAME = 3840


class VADState(Enum):
    """Voice activity detection state machine."""

    IDLE = auto()
    SPEAKING = auto()
    TRAILING_SILENCE = auto()


@dataclass
class VADConfig:
    """Configuration for voice activity detection."""

    energy_threshold: float = 150.0
    silence_duration_ms: int = 500
    min_speech_duration_ms: int = 200
    max_speech_duration_ms: int = 30_000

    @property
    def silence_frames(self) -> int:
        """Number of silence frames before utterance is considered complete."""
        return self.silence_duration_ms // FRAME_DURATION_MS

    @property
    def min_speech_frames(self) -> int:
        return self.min_speech_duration_ms // FRAME_DURATION_MS

    @property
    def max_speech_frames(self) -> int:
        return self.max_speech_duration_ms // FRAME_DURATION_MS


def rms_energy(pcm_data: bytes) -> float:
    """Calculate RMS energy of 16-bit PCM audio.

    Args:
        pcm_data: Raw 16-bit signed LE PCM bytes.

    Returns:
        RMS energy value (0.0 for silence, ~32k for max amplitude).
    """
    if len(pcm_data) < 2:
        return 0.0
    n_samples = len(pcm_data) // 2
    samples = struct.unpack(f"<{n_samples}h", pcm_data[:n_samples * 2])
    return math.sqrt(sum(s * s for s in samples) / n_samples)


@dataclass
class UserAudioBuffer:
    """Accumulates PCM frames for a single user with VAD.

    Thread-safe for single-writer (Discord voice thread).

    Args:
        user_id: Discord user ID.
        config: VAD configuration.
        on_utterance: Callback invoked with (user_id, pcm_bytes) when
            a complete utterance is detected. Called from the voice thread.
    """

    user_id: int
    config: VADConfig = field(default_factory=VADConfig)
    on_utterance: Callable[[int, bytes], None] | None = None

    _state: VADState = field(default=VADState.IDLE, init=False)
    _frames: list[bytes] = field(default_factory=list, init=False)
    _speech_frame_count: int = field(default=0, init=False)
    _silence_frame_count: int = field(default=0, init=False)
    _last_frame_time: float = field(default=0.0, init=False)
    # Pre-buffer: captures audio BEFORE speech is detected.
    # Plosive consonants (P, B, T) are often below the energy threshold,
    # so "Poob" gets clipped to "oob" or missed entirely. The pre-buffer
    # ensures the onset is always captured.
    _pre_buffer: list[bytes] = field(default_factory=list, init=False)
    _PRE_BUFFER_FRAMES: int = 5  # 5 frames × 20ms = 100ms of lookback (enough for onset)

    def add_frame(self, pcm_frame: bytes) -> None:
        """Process a single PCM frame (20ms, called from voice thread).

        Args:
            pcm_frame: Raw PCM bytes from Discord (48kHz 16-bit stereo).
        """
        now = time.monotonic()

        # Packet gap detection: if we're in SPEAKING state and there was a
        # long gap since the last frame, Discord's own VAD decided the user
        # stopped talking. Emit the utterance immediately.
        if self._state == VADState.SPEAKING and self._last_frame_time > 0:
            gap_ms = (now - self._last_frame_time) * 1000
            if gap_ms > 300:  # Discord stopped sending for 300ms+
                self._emit_utterance()

        self._last_frame_time = now
        energy = rms_energy(pcm_frame)
        is_speech = energy >= self.config.energy_threshold

        match self._state:
            case VADState.IDLE:
                # Always maintain a rolling pre-buffer of recent frames
                self._pre_buffer.append(pcm_frame)
                if len(self._pre_buffer) > self._PRE_BUFFER_FRAMES:
                    self._pre_buffer.pop(0)

                if is_speech:
                    self._state = VADState.SPEAKING
                    # Prepend pre-buffer to capture speech onset (plosive consonants)
                    self._frames = list(self._pre_buffer)
                    self._pre_buffer.clear()
                    self._speech_frame_count = 1
                    self._silence_frame_count = 0

            case VADState.SPEAKING:
                self._frames.append(pcm_frame)
                if is_speech:
                    self._speech_frame_count += 1
                    self._silence_frame_count = 0
                else:
                    self._silence_frame_count += 1
                    if self._silence_frame_count >= self.config.silence_frames:
                        self._emit_utterance()

                # Hard cutoff for very long speech
                if self._speech_frame_count >= self.config.max_speech_frames:
                    self._emit_utterance()

            case VADState.TRAILING_SILENCE:
                self._state = VADState.IDLE

    def _emit_utterance(self) -> None:
        """Emit the buffered audio as a complete utterance."""
        if self._speech_frame_count < self.config.min_speech_frames:
            log.debug(
                "Utterance too short, discarding",
                user=self.user_id,
                frames=self._speech_frame_count,
            )
            self._reset()
            return

        pcm_data = b"".join(self._frames)
        duration_ms = len(self._frames) * FRAME_DURATION_MS
        log.info(
            "Utterance complete",
            user=self.user_id,
            duration_ms=duration_ms,
            frames=len(self._frames),
        )

        if self.on_utterance:
            self.on_utterance(self.user_id, pcm_data)

        self._reset()

    def _reset(self) -> None:
        """Reset state machine to IDLE."""
        self._state = VADState.IDLE
        self._frames = []
        self._speech_frame_count = 0
        self._silence_frame_count = 0

    def flush(self) -> bytes | None:
        """Force-flush any buffered audio (e.g., on disconnect).

        Returns:
            PCM bytes if there was speech buffered, None otherwise.
        """
        if self._state != VADState.IDLE and self._frames:
            pcm_data = b"".join(self._frames)
            self._reset()
            return pcm_data
        return None
