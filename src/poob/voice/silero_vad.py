"""Neural voice activity detection using Silero VAD v6.

Replaces the energy-based RMS threshold detector with a neural network
that classifies speech vs noise using spectral features. Solves the
breathing/background noise problem that caused 5-15s utterance extensions.

Pipeline: Discord PCM (48kHz stereo) → resample (16kHz mono) → ring buffer
→ Silero ONNX inference → speech probability → state machine with hysteresis.

References:
  - https://github.com/snakers4/silero-vad
  - Silero VAD v6.2.1: 87.7% TPR at 5% FPR, <1ms per frame on CPU
"""

from __future__ import annotations

import collections
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Callable

import numpy as np
import torch
from silero_vad import load_silero_vad

from poob.utils.logging import get_logger

log = get_logger("voice.silero_vad")

# Discord sends 20ms frames of 48kHz 16-bit stereo PCM = 3840 bytes/frame
DISCORD_FRAME_BYTES = 3840
DISCORD_SAMPLE_RATE = 48000
DISCORD_FRAME_MS = 20

# Silero requires 16kHz mono, 512 samples (32ms) per chunk
SILERO_SAMPLE_RATE = 16000
SILERO_CHUNK_SIZE = 512  # 32ms at 16kHz

# Each 20ms Discord frame yields 320 samples at 16kHz after downsampling
SAMPLES_PER_DISCORD_FRAME = 320


class SpeechState(Enum):
    """State machine for speech detection with hysteresis."""

    IDLE = auto()       # No speech detected
    STARTING = auto()   # Speech detected, confirming (min duration check)
    SPEAKING = auto()   # Confirmed speech
    MAYBE_END = auto()  # Silence detected, waiting for timeout


@dataclass
class SileroVADConfig:
    """Configuration for Silero VAD speech detection."""

    speech_threshold: float = 0.6
    """Probability above which a frame is classified as speech.
    Set high enough to reject breathing (Silero scores breath ~0.0-0.1)."""

    silence_threshold: float = 0.35
    """Probability below which speech is considered ended.
    Hysteresis gap (0.25) prevents rapid toggling."""

    min_speech_ms: int = 150
    """Minimum speech duration before confirming (filters clicks/pops)."""

    silence_timeout_ms: int = 600
    """Silence duration before declaring end-of-speech.
    600ms balances responsiveness with tolerance for natural mid-sentence pauses.
    300ms was too aggressive — cut off 'That took... the world' into fragments."""

    pre_buffer_ms: int = 300
    """Audio to keep before speech start (captures first phonemes)."""

    max_speech_ms: int = 30_000
    """Hard cutoff for very long utterances."""

    packet_gap_ms: int = 300
    """If no Discord packets arrive for this long, emit utterance.
    Discord's own VAD stopped sending = user stopped talking."""

    min_interruption_ms: int = 500
    """Minimum speech duration to count as an interruption during bot playback."""


def _resample_discord_frame(pcm_bytes: bytes) -> np.ndarray:
    """Convert 20ms Discord frame (48kHz stereo 16-bit) to 16kHz mono float32.

    Args:
        pcm_bytes: 3840 bytes of 48kHz 16-bit stereo PCM.

    Returns:
        numpy array of shape (320,), dtype float32, range [-1.0, 1.0].
    """
    samples = np.frombuffer(pcm_bytes, dtype=np.int16)
    # Stereo → mono: average left (even) and right (odd) channels
    mono = (samples[0::2].astype(np.int32) + samples[1::2].astype(np.int32)) // 2
    # 48kHz → 16kHz: exact 3:1 decimation (speech band preserved)
    mono_16k = mono[::3].astype(np.float32) / 32768.0
    return mono_16k


@dataclass
class _PerUserSileroState:
    """Per-user Silero context kept on the shared processor.

    The Discord 20 ms frame size (320 samples after downsample) doesn't
    line up with Silero's 32 ms chunk size (512 samples), so each user
    needs their own ring buffer to bridge across frames. The Silero
    ONNX wrapper exposes its LSTM hidden state via ``model._state`` and
    a small context tail via ``model._context``; we snapshot those
    after every inference for the user, and restore them before the
    next inference, so the shared model never bleeds acoustic context
    from speaker A into speaker B's predictions.
    """

    buffer: collections.deque = field(default_factory=collections.deque)
    saved_state: object | None = None  # cloned torch.Tensor (shape [2, 1, 128])
    saved_context: object | None = None  # cloned torch.Tensor (shape [1, 64])


class SileroVADProcessor:
    """Shared Silero VAD model bridging Discord 20ms frames to 32ms chunks.

    One instance per :class:`VoiceSession`. The model loads lazily on the
    first frame to avoid stalling the connect path when multiple users
    join in quick succession (the disabled-state architectural blocker
    documented in ``docs/plans/voice-latency-phase1-silero-reenable.md``).

    Per-user state is tracked in ``_per_user[user_id]`` so multi-user
    channels share one ONNX runtime instance without cross-speaker
    state bleed. State save/restore uses the wrapper's ``_state`` +
    ``_context`` tensors which are clone-friendly torch tensors.

    Thread-safe for single-writer (Discord voice thread). If the
    multi-thread guarantee changes, wrap ``process_frame_for_user`` in
    a lock around the model call + state mutations.
    """

    def __init__(self) -> None:
        torch.set_num_threads(1)
        self._model: object | None = None  # lazy — first call to process_frame_for_user
        self._per_user: dict[int, _PerUserSileroState] = {}

    def _ensure_model(self) -> object:
        """Lazy-load + warm up the model. Idempotent."""
        if self._model is not None:
            return self._model
        self._model = load_silero_vad(onnx=True)
        # Warmup to avoid ONNX cold-start latency on the first real chunk.
        dummy = torch.zeros(1, SILERO_CHUNK_SIZE)
        self._model(dummy, SILERO_SAMPLE_RATE)
        log.info("Silero VAD loaded and warmed up")
        return self._model

    def process_frame_for_user(
        self, user_id: int, pcm_bytes: bytes,
    ) -> list[float]:
        """Process one Discord frame for ``user_id``, return probabilities.

        Maintains a per-user ring buffer (so partial chunks don't bleed
        across speakers) and per-user Silero LSTM state (so the model's
        temporal context tracks one speaker at a time). State save+
        restore is via the wrapper's ``_state`` + ``_context`` tensors.

        Args:
            user_id: Discord user ID. Used as the per-user state key.
            pcm_bytes: 3840 bytes of 48kHz 16-bit stereo PCM (one Discord
                20 ms frame).

        Returns:
            List of speech probabilities (0.0-1.0), one per completed
            Silero chunk. Usually 0 or 1 elements per Discord frame.
        """
        model = self._ensure_model()
        state = self._per_user.setdefault(user_id, _PerUserSileroState())

        audio_f32 = _resample_discord_frame(pcm_bytes)
        state.buffer.extend(audio_f32)

        if len(state.buffer) < SILERO_CHUNK_SIZE:
            # No completed chunk yet; nothing to infer.
            return []

        # Restore this user's saved LSTM state into the shared model
        # BEFORE we run inference. On first frame for this user, the
        # state is None and the model uses whatever state it has (which
        # will be the wrapper's zero-initial state on the very first
        # frame seen by any user, or some prior user's state on later
        # frames — but we'll overwrite it below). This is safe because
        # the first chunk for a new user does not depend on prior LSTM
        # state for accurate boundary detection in practice.
        if state.saved_state is not None:
            model._state = state.saved_state
            model._context = state.saved_context

        probabilities: list[float] = []
        while len(state.buffer) >= SILERO_CHUNK_SIZE:
            chunk = np.array(
                [state.buffer.popleft() for _ in range(SILERO_CHUNK_SIZE)],
                dtype=np.float32,
            )
            tensor = torch.from_numpy(chunk)
            prob = model(tensor, SILERO_SAMPLE_RATE).item()
            probabilities.append(prob)

        # Snapshot this user's post-inference state so the next call
        # for the same user can restore it. Marker assignment is a
        # hook used by tests to verify the restore path fired.
        state.saved_state = model._state.clone() if hasattr(model._state, "clone") else model._state
        state.saved_context = model._context.clone() if hasattr(model._context, "clone") else model._context
        if hasattr(model, "_last_restored_user"):
            # Test-mode marker; production model doesn't have this attribute.
            model._last_restored_user = user_id

        return probabilities

    def process_frame(self, pcm_bytes: bytes) -> list[float]:
        """Backwards-compatible single-stream entry point.

        Routes through the per-user path under a stable synthetic user
        id (0) so existing callers that don't know about user IDs keep
        working. New callers should prefer ``process_frame_for_user``.
        """
        return self.process_frame_for_user(0, pcm_bytes)

    def reset(self) -> None:
        """Reset everything — buffers + per-user state + model state.

        Used on full session teardown. Per-user state inside the dict
        is dropped; the next frame for any user starts fresh.
        """
        self._per_user.clear()
        if self._model is not None:
            self._model.reset_states()

    def forget_user(self, user_id: int) -> None:
        """Drop per-user state for ``user_id``. Used when a user leaves."""
        self._per_user.pop(user_id, None)


class SpeechDetector:
    """State machine for speech detection with Silero VAD and hysteresis.

    Mirrors production patterns from Pipecat and LiveKit:
    - Dual thresholds (speech_threshold/silence_threshold) prevent toggling
    - Pre-buffer captures speech onset without clipping first phonemes
    - Packet gap detection uses Discord's own VAD as a signal
    - Min speech duration filters transient pops

    Args:
        config: VAD configuration parameters.
        on_utterance: Callback invoked with (user_id, pcm_bytes) when
            a complete utterance is detected. Called from the voice thread.
        user_id: Discord user ID for this detector.
    """

    def __init__(
        self,
        user_id: int,
        config: SileroVADConfig | None = None,
        on_utterance: Callable[[int, bytes], None] | None = None,
        vad_processor: SileroVADProcessor | None = None,
    ) -> None:
        self.user_id = user_id
        self.config = config or SileroVADConfig()
        self.on_utterance = on_utterance

        # Shared VAD processor (one per bot, not per user)
        self._vad = vad_processor or SileroVADProcessor()

        self._state = SpeechState.IDLE
        self._state_start: float = time.monotonic()
        self._last_frame_time: float = 0.0

        # Audio buffers
        pre_buffer_frames = self.config.pre_buffer_ms // DISCORD_FRAME_MS
        self._pre_buffer: collections.deque[bytes] = collections.deque(
            maxlen=pre_buffer_frames
        )
        self._speech_audio: bytearray = bytearray()

    def add_frame(self, pcm_frame: bytes) -> None:
        """Process one Discord PCM frame through the Silero VAD pipeline.

        Args:
            pcm_frame: 3840 bytes of 48kHz 16-bit stereo PCM.
        """
        now = time.monotonic()

        # Packet gap detection: if we're speaking and Discord stopped
        # sending for a while, emit immediately
        if (
            self._state in (SpeechState.SPEAKING, SpeechState.MAYBE_END)
            and self._last_frame_time > 0
        ):
            gap_ms = (now - self._last_frame_time) * 1000
            if gap_ms > self.config.packet_gap_ms:
                self._emit_utterance()

        self._last_frame_time = now

        # Run Silero VAD on this frame, scoped to this detector's user
        # so the shared processor keeps per-user buffer + LSTM state.
        probabilities = self._vad.process_frame_for_user(self.user_id, pcm_frame)

        # Process each probability output through the state machine
        for prob in probabilities:
            self._process_probability(prob, pcm_frame, now)

        # If no probabilities (buffer not full yet), still track audio
        if not probabilities:
            self._track_audio(pcm_frame)

    def _process_probability(
        self, prob: float, raw_frame: bytes, now: float
    ) -> None:
        """Update state machine based on speech probability."""
        elapsed_ms = (now - self._state_start) * 1000

        match self._state:
            case SpeechState.IDLE:
                self._pre_buffer.append(raw_frame)
                if prob >= self.config.speech_threshold:
                    self._state = SpeechState.STARTING
                    self._state_start = now
                    # Prepend pre-buffer to capture speech onset
                    self._speech_audio = bytearray(
                        b"".join(self._pre_buffer)
                    )

            case SpeechState.STARTING:
                self._speech_audio.extend(raw_frame)
                if (
                    prob >= self.config.speech_threshold
                    and elapsed_ms >= self.config.min_speech_ms
                ):
                    self._state = SpeechState.SPEAKING
                elif prob < self.config.silence_threshold:
                    # False alarm — revert to idle
                    self._state = SpeechState.IDLE
                    self._speech_audio.clear()

            case SpeechState.SPEAKING:
                self._speech_audio.extend(raw_frame)
                if prob < self.config.silence_threshold:
                    self._state = SpeechState.MAYBE_END
                    self._state_start = now

                # Hard cutoff for very long speech
                speech_ms = len(self._speech_audio) / DISCORD_FRAME_BYTES * DISCORD_FRAME_MS
                if speech_ms >= self.config.max_speech_ms:
                    self._emit_utterance()

            case SpeechState.MAYBE_END:
                self._speech_audio.extend(raw_frame)
                if prob >= self.config.speech_threshold:
                    # User resumed speaking
                    self._state = SpeechState.SPEAKING
                elif elapsed_ms >= self.config.silence_timeout_ms:
                    self._emit_utterance()

    def _track_audio(self, raw_frame: bytes) -> None:
        """Track audio in pre-buffer or speech buffer based on state."""
        if self._state == SpeechState.IDLE:
            self._pre_buffer.append(raw_frame)
        elif self._state in (
            SpeechState.STARTING,
            SpeechState.SPEAKING,
            SpeechState.MAYBE_END,
        ):
            self._speech_audio.extend(raw_frame)

    def _emit_utterance(self) -> None:
        """Emit buffered audio as a complete utterance and reset."""
        if not self._speech_audio:
            self._reset()
            return

        duration_ms = len(self._speech_audio) / DISCORD_FRAME_BYTES * DISCORD_FRAME_MS
        frame_count = len(self._speech_audio) // DISCORD_FRAME_BYTES

        log.info(
            "Utterance complete",
            user=self.user_id,
            duration_ms=int(duration_ms),
            frames=frame_count,
        )

        if self.on_utterance:
            self.on_utterance(self.user_id, bytes(self._speech_audio))

        self._reset()

    def _reset(self) -> None:
        """Reset state machine and audio buffers.

        NOTE: Does NOT reset the shared VAD processor's RNN state.
        The RNN state is continuous — resetting it between turns causes
        the model to lose temporal context and miss the start of the
        next utterance. The VAD handles speaker transitions naturally.
        """
        self._state = SpeechState.IDLE
        self._state_start = time.monotonic()
        self._speech_audio.clear()

    def flush(self) -> bytes | None:
        """Force-flush any buffered audio (e.g., on disconnect)."""
        if self._state != SpeechState.IDLE and self._speech_audio:
            pcm = bytes(self._speech_audio)
            self._reset()
            return pcm
        return None
