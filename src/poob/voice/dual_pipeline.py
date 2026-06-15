"""Dual-pipeline audio processor: wake word detection + streaming STT in parallel.

Runs two parallel paths on each user's audio:
1. Porcupine wake word detector — detects "Hey Poob" from raw audio in ~50ms
2. Deepgram streaming STT — transcribes speech in real-time via WebSocket

When Porcupine detects the wake word, the Deepgram transcript is already
available (or nearly so). This eliminates both the wake word recognition
problem (Porcupine works on raw audio, not text) and the STT latency
bottleneck (transcript builds while user speaks, not after).

Architecture:
    Discord audio (48kHz stereo PCM per user)
        → Downsample to 16kHz mono
        ├→ Porcupine: wake word detected? → flip is_active flag
        └→ Deepgram WebSocket: streaming transcript building
             → When speech ends + is_active: full transcript ready instantly
"""

from __future__ import annotations

import asyncio
import collections
import os
import struct
import threading
import time
import wave
from dataclasses import dataclass, field
from typing import Callable

import numpy as np

from poob.utils.logging import get_logger

log = get_logger("voice.dual_pipeline")

# Audio constants
DISCORD_SAMPLE_RATE = 48000
DISCORD_CHANNELS = 2
TARGET_SAMPLE_RATE = 16000
FRAME_MS = 20
DISCORD_FRAME_BYTES = DISCORD_SAMPLE_RATE * DISCORD_CHANNELS * 2 * FRAME_MS // 1000  # 3840


def downsample_48k_stereo_to_16k_mono(pcm_stereo: bytes) -> bytes:
    """Convert Discord 48kHz stereo PCM to 16kHz mono for wake word + STT.

    Uses numpy for fast stereo→mono averaging and integer decimation (÷3).
    Anti-aliasing is not applied (acceptable for speech; saves ~1ms/frame).

    Args:
        pcm_stereo: Raw 16-bit signed LE stereo PCM at 48kHz.

    Returns:
        Raw 16-bit signed LE mono PCM at 16kHz.
    """
    if len(pcm_stereo) < 4:
        return b""
    samples = np.frombuffer(pcm_stereo, dtype=np.int16)
    # Stereo to mono (average L+R channels)
    mono = samples.reshape(-1, 2).mean(axis=1).astype(np.int16)
    # Downsample 48kHz → 16kHz (take every 3rd sample)
    downsampled = mono[::3]
    return downsampled.tobytes()


@dataclass
class UserPipeline:
    """Per-user dual pipeline state.

    Each user in the voice channel gets their own Porcupine instance
    and Deepgram WebSocket connection.
    """
    user_id: int
    user_name: str = ""

    # Wake word state
    is_active: bool = False  # True when wake word detected in current utterance
    wake_word_time: float = 0.0  # When wake word was last detected
    _pending_wake: bool = False  # Wake word fired during silence — carry to next utterance
    _pending_wake_time: float = 0.0  # When pending wake word fired (expires after 3s)

    # Streaming transcript state
    current_transcript: str = ""  # Accumulates during speech
    transcript_final: bool = False  # True when Deepgram sends speech_final
    speech_started: bool = False  # True when user is currently speaking

    # Timing
    last_audio_time: float = field(default_factory=time.monotonic)
    speech_start_time: float = 0.0


class WakeWordDetector:
    """Wake word detection using OpenWakeWord (local, open source).

    Uses pre-trained models or custom-trained models to detect wake words
    directly from raw 16kHz mono audio. No API keys, no cloud calls.

    For testing: uses "hey_jarvis" pre-trained model.
    For production: swap in a custom "hey_poob" trained model.
    """

    def __init__(self, model_path: str | None = None, threshold: float = 0.5) -> None:
        """Initialize wake word detector.

        Args:
            model_path: Path to custom .onnx wake word model.
                If None, uses pre-trained "hey_jarvis" for testing.
            threshold: Detection threshold (0.0-1.0). Higher = fewer false positives.
        """
        self._model_path = model_path
        self._threshold = threshold
        self._model = None
        self._model_name = ""
        self._audio_buffers: dict[int, bytearray] = {}  # per-user audio accumulation
        self._FRAME_SAMPLES = 1280  # 80ms at 16kHz — OpenWakeWord's expected chunk size

    def _ensure_model(self) -> None:
        """Lazy-load the OpenWakeWord model."""
        if self._model is not None:
            return

        from openwakeword.model import Model

        if self._model_path:
            self._model = Model(
                wakeword_models=[self._model_path],
                inference_framework="onnx",
            )
            self._model_name = "custom"
        else:
            # Use pre-trained "hey_jarvis" for testing
            self._model = Model(inference_framework="onnx")
            self._model_name = "hey_jarvis"

        log.info(
            "OpenWakeWord loaded",
            model=self._model_name,
            threshold=self._threshold,
        )

    def process_frame(self, user_id: int, pcm_16k_mono: bytes) -> bool:
        """Process a 16kHz mono PCM frame for wake word detection.

        Accumulates audio into 80ms chunks (1280 samples) as required
        by OpenWakeWord, then runs inference.

        Args:
            user_id: Discord user ID.
            pcm_16k_mono: Raw 16-bit signed LE mono PCM at 16kHz.

        Returns:
            True if wake word was detected.
        """
        self._ensure_model()

        # Accumulate audio for this user
        if user_id not in self._audio_buffers:
            self._audio_buffers[user_id] = bytearray()
        self._audio_buffers[user_id].extend(pcm_16k_mono)

        # Process all complete 80ms chunks
        detected = False
        buf = self._audio_buffers[user_id]
        chunk_bytes = self._FRAME_SAMPLES * 2  # 16-bit = 2 bytes per sample

        while len(buf) >= chunk_bytes:
            chunk = bytes(buf[:chunk_bytes])
            del buf[:chunk_bytes]

            # Convert to int16 numpy array
            samples = np.frombuffer(chunk, dtype=np.int16)

            # Run inference
            prediction = self._model.predict(samples)

            # Check all model outputs against threshold
            for name, score in prediction.items():
                s = float(score)
                if s >= self._threshold:
                    detected = True
                    # Don't log here — caller logs once per detection event

        return detected

    def reset_user(self, user_id: int) -> None:
        """Clear per-user audio accumulation buffer between utterances.

        Does NOT call model.reset() — OpenWakeWord's global model state
        (mel spectrogram buffer, embedding cache, RNN hidden state) must
        persist across utterances. Resetting it destroys the feature context
        the model needs to detect wake words in subsequent utterances.
        The wake word classifier's prediction scores naturally decay to 0
        after the phrase passes, so stale activations are not a concern.
        """
        self._audio_buffers.pop(user_id, None)

    def cleanup(self) -> None:
        """Release resources."""
        self._audio_buffers.clear()
        self._model = None


class _UserStream:
    """State for a single user's Deepgram WebSocket stream."""

    def __init__(self) -> None:
        self.ws = None  # WebSocket connection
        self.transcript: str = ""  # Current accumulated transcript (is_final segments)
        self.latest_interim: str = ""  # Most recent interim (partial) result
        self.is_final: bool = False  # True when speech_final received
        self.connected: bool = False
        self.listener_task = None  # Background task reading from WS
        self._audio_queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._sender_task = None
        # Per-utterance sequence numbers — the double-emit SEAL (2026-06-11).
        # utterance_seq bumps when Deepgram closes an utterance (speech_final /
        # UtteranceEnd). transcript_seq is the utterance the current transcript
        # belongs to. emitted_seq is the highest utterance we've already
        # emitted. get_transcript returns "" while transcript_seq <= emitted_seq,
        # so Deepgram re-appends after our VAD emit+reset can't rebuild the
        # phrase and double-fire. See vc-session-failures-2026-06-11-rootcause.
        self.utterance_seq: int = 0
        self.transcript_seq: int = 0
        self.emitted_seq: int = -1


class DeepgramStreamManager:
    """Manages per-user Deepgram streaming WebSocket connections.

    Each user gets a persistent WebSocket that receives audio chunks
    and returns interim/final transcripts in real-time. Transcripts
    build incrementally as the user speaks — by the time they finish,
    the full text is already available (or nearly so).

    Uses raw websockets for maximum control and reliability.
    """

    # Frames buffered per user while a stream is down/reconnecting, flushed
    # in order once the socket is back. ~150 × 20ms ≈ 3s — bounds memory and
    # staleness (buffering only happens during active speech, so the buffer
    # holds recent contiguous audio, not gaps). See
    # docs/plans/voice-pipeline-reliability.md (Issue 3).
    _PENDING_MAX_FRAMES: int = 150

    # Zombie-stream recovery: a Deepgram socket can report connected=True yet
    # silently deliver no transcripts (half-open socket / Deepgram-side stall).
    # The passive reconnect only fires on connected=False, so a zombie never
    # self-heals — the user goes deaf to Poob until they rejoin or the bot
    # restarts. After this many consecutive wake-fired-but-no-transcript misses
    # for a user, force-rebuild their stream. See
    # docs/incidents/deepgram-zombie-stream-no-transcript.md.
    _ZOMBIE_LOST_THRESHOLD: int = 2

    def __init__(self, api_key: str, model: str = "nova-3") -> None:
        self._api_key = api_key
        self._model = model
        self._streams: dict[int, _UserStream] = {}
        self._connect_locks: dict[int, asyncio.Lock] = {}
        self._last_connect_time: dict[int, float] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._keepalive_task: asyncio.Task | None = None
        self._pending_audio: dict[int, collections.deque[bytes]] = {}
        # Per-user consecutive "wake fired but no transcript" misses; reset to
        # 0 the instant any transcript arrives. Drives zombie-stream recovery.
        self._consecutive_lost: dict[int, int] = {}

    def _buffer_pending(self, user_id: int, frame: bytes) -> None:
        """Queue an audio frame that couldn't be sent (stream down /
        reconnecting / throttled) so it survives until the socket is back.
        Bounded ring — past the cap the oldest frame is dropped, keeping
        the most recent ~3s of the current utterance."""
        buf = self._pending_audio.get(user_id)
        if buf is None:
            buf = self._pending_audio[user_id] = collections.deque(
                maxlen=self._PENDING_MAX_FRAMES,
            )
        buf.append(frame)

    async def _flush_pending(self, user_id: int, stream: "_UserStream") -> None:
        """Send any buffered frames in order over the (now-connected) socket.
        On send failure, mark disconnected and re-buffer the failed frame so
        the next reconnect retries it."""
        buf = self._pending_audio.get(user_id)
        if not buf:
            return
        while buf:
            frame = buf.popleft()
            try:
                await stream.ws.send(frame)
            except Exception:
                stream.connected = False
                buf.appendleft(frame)
                return

    def start_keepalive_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Start a background task that sends keepalive to all active streams.

        This runs independently of audio frames — even when no one is speaking,
        the WebSocket stays alive. Prevents the Deepgram 1011 disconnect.
        """
        async def _keepalive_forever():
            import json as _json
            while True:
                await asyncio.sleep(5)
                for user_id, stream in list(self._streams.items()):
                    if stream.connected and stream.ws:
                        try:
                            await stream.ws.send(_json.dumps({"type": "KeepAlive"}))
                        except Exception:
                            stream.connected = False

        self._keepalive_task = loop.create_task(_keepalive_forever())

    def _get_url(self) -> str:
        """Build the Deepgram streaming WebSocket URL with params."""
        return (
            f"wss://api.deepgram.com/v1/listen"
            f"?model={self._model}"
            f"&language=en"
            f"&encoding=linear16"
            f"&sample_rate=16000"
            f"&channels=1"
            f"&punctuate=true"
            f"&smart_format=true"
            f"&interim_results=true"
            f"&utterance_end_ms=1500"
            f"&vad_events=true"
            f"&keyterm=Poob"
            f"&keyterm=play"
            f"&keyterm=skip"
            f"&keyterm=pause"
            f"&keyterm=stop"
            f"&keyterm=queue"
            f"&keyterm=volume"
            f"&keyterm=shuffle"
        )

    async def _connect_user(self, user_id: int) -> _UserStream:
        """Create or get a streaming connection for a user."""
        if user_id in self._streams and self._streams[user_id].connected:
            return self._streams[user_id]

        stream = _UserStream()
        self._streams[user_id] = stream

        try:
            import websockets

            headers = {"Authorization": f"Token {self._api_key}"}
            stream.ws = await asyncio.wait_for(
                websockets.connect(
                    self._get_url(),
                    additional_headers=headers,
                    ping_interval=20,
                    ping_timeout=10,
                ),
                timeout=10.0,
            )
            stream.connected = True

            # Start background listener for transcript updates
            stream.listener_task = asyncio.create_task(
                self._listen_loop(user_id, stream)
            )

            log.info("Deepgram stream connected", user=user_id)
        except Exception as exc:
            log.warning("Deepgram stream connect failed", user=user_id, error=str(exc)[:100])
            stream.connected = False

        return stream

    async def _listen_loop(self, user_id: int, stream: _UserStream) -> None:
        """Background task: read transcript updates from Deepgram WebSocket."""
        import json as _json

        try:
            async for msg in stream.ws:
                try:
                    data = _json.loads(msg)
                except (ValueError, TypeError):
                    continue

                msg_type = data.get("type", "")

                if msg_type == "Results":
                    channel = data.get("channel", {})
                    alts = channel.get("alternatives", [{}])
                    transcript = alts[0].get("transcript", "") if alts else ""
                    is_final = data.get("is_final", False)
                    speech_final = data.get("speech_final", False)

                    if transcript:
                        # Any transcript = the stream is alive; clear the
                        # zombie-miss counter for this user.
                        self._consecutive_lost[user_id] = 0
                        # First content of a NEW utterance (our speech-start
                        # bumped utterance_seq via begin_utterance): start the
                        # transcript fresh + tag it with the new seq so it never
                        # appends onto a sealed, already-emitted one. See the
                        # SEAL on _UserStream.
                        if stream.utterance_seq != stream.transcript_seq:
                            stream.transcript = ""
                            stream.latest_interim = ""
                            stream.transcript_seq = stream.utterance_seq
                        if is_final:
                            # Append final segment to accumulated transcript
                            if stream.transcript:
                                stream.transcript += " " + transcript
                            else:
                                stream.transcript = transcript
                            stream.latest_interim = ""
                        else:
                            # Interim result — save as latest partial for fallback
                            stream.latest_interim = transcript
                        if speech_final:
                            stream.is_final = True

                elif msg_type == "UtteranceEnd":
                    stream.is_final = True

        except Exception as exc:
            log.warning("Deepgram listener ended", user=user_id, error=str(exc)[:80])
        finally:
            log.warning("Deepgram stream disconnected", user=user_id)
            stream.connected = False

    async def send_audio(self, user_id: int, pcm_16k_mono: bytes) -> None:
        """Send audio chunk to user's Deepgram stream.

        Creates connection on first call. Uses a per-user lock to prevent
        concurrent reconnection attempts from racing.

        Args:
            user_id: Discord user ID.
            pcm_16k_mono: Raw 16-bit signed LE mono PCM at 16kHz.
        """
        stream = self._streams.get(user_id)

        if stream is None or not stream.connected:
            # Stream down. Buffer this frame so the start of the utterance
            # isn't lost to the reconnect window, then attempt a (throttled,
            # lazy) reconnect. The throttle + lazy-on-audio cost guard is
            # preserved — buffering only prevents data loss within it.
            self._buffer_pending(user_id, pcm_16k_mono)

            if user_id not in self._connect_locks:
                self._connect_locks[user_id] = asyncio.Lock()

            lock = self._connect_locks[user_id]
            if lock.locked():
                return  # Another coroutine is connecting — it will flush the buffer

            async with lock:
                stream = self._streams.get(user_id)
                if stream is not None and stream.connected:
                    pass  # Connected while we waited for lock
                else:
                    # Throttle reconnects to once per 5 seconds
                    now = asyncio.get_event_loop().time()
                    last = self._last_connect_time.get(user_id, 0.0)
                    if now - last < 5.0:
                        return  # Frame is buffered; flushed when reconnect lands
                    self._last_connect_time[user_id] = now
                    if stream is not None:
                        log.info("Deepgram stream reconnecting", user=user_id)
                    stream = await self._connect_user(user_id)

                if stream is not None and stream.connected and stream.ws is not None:
                    # Back up — flush the buffered frames (includes this one).
                    await self._flush_pending(user_id, stream)
            return

        # Already connected — drain any stragglers first to keep order, then
        # send the current frame.
        if self._pending_audio.get(user_id):
            await self._flush_pending(user_id, stream)
        try:
            await stream.ws.send(pcm_16k_mono)
        except Exception:
            stream.connected = False
            self._buffer_pending(user_id, pcm_16k_mono)

    def get_transcript(self, user_id: int) -> tuple[str, bool]:
        """Get current transcript for a user.

        Returns finalized transcript if available, otherwise falls back
        to the latest interim (partial) result. This prevents losing
        transcripts when our VAD fires before Deepgram sends is_final.

        Returns:
            (transcript_text, is_final) where is_final means the user
            finished speaking and the transcript is complete.
        """
        stream = self._streams.get(user_id)
        if stream is None:
            return "", False
        # SEAL: once an utterance is emitted (mark_emitted), ignore content that
        # still belongs to it. Deepgram re-appends after our VAD emit+reset would
        # otherwise rebuild the phrase and double-fire (2026-06-11 double-queue).
        # A genuine NEW utterance bumps utterance_seq -> transcript_seq, un-
        # sealing. Covers BOTH transcript and the latest_interim fallback below.
        if stream.transcript_seq <= stream.emitted_seq:
            return "", stream.is_final
        # Prefer finalized transcript, fall back to interim
        text = stream.transcript
        if not text and stream.latest_interim:
            text = stream.latest_interim
        return text, stream.is_final

    def mark_emitted(self, user_id: int) -> None:
        """Seal the just-emitted utterance: get_transcript returns "" for it
        until a NEW utterance arrives (utterance_seq bump). Closes the
        re-accumulation double-fire at the source — see vc-session-failures-
        2026-06-11-rootcause. GIL-atomic int compare, so safe vs the listener
        thread's appends without a lock."""
        stream = self._streams.get(user_id)
        if stream:
            stream.emitted_seq = stream.transcript_seq

    def begin_utterance(self, user_id: int) -> None:
        """Mark the start of a NEW utterance (called from our speech-start
        detection — reliable, unlike Deepgram's UtteranceEnd). Bumps
        utterance_seq so the next Deepgram content is tagged fresh and un-seals
        get_transcript. This is the discriminator: a genuine re-request means
        the user SPOKE AGAIN (new speech-start -> new utterance -> emits), while
        Deepgram re-appending to the SAME utterance after our emit stays sealed.
        See vc-session-failures-2026-06-11-rootcause."""
        stream = self._streams.get(user_id)
        if stream:
            stream.utterance_seq += 1

    def reset_transcript(self, user_id: int) -> None:
        """Clear transcript state for a user (after processing)."""
        stream = self._streams.get(user_id)
        if stream:
            stream.transcript = ""
            stream.latest_interim = ""
            stream.is_final = False

    def note_transcript_delivered(self, user_id: int) -> None:
        """Mark that a transcript was delivered — the stream is healthy, so
        clear the zombie-miss counter."""
        self._consecutive_lost[user_id] = 0

    async def report_lost_transcript(self, user_id: int) -> bool:
        """Record a wake-fired-but-no-transcript miss for a user.

        After ``_ZOMBIE_LOST_THRESHOLD`` consecutive misses the stream is
        treated as a zombie (socket reports connected but delivers nothing) and
        force-reconnected. Returns True if a recovery was triggered.
        """
        n = self._consecutive_lost.get(user_id, 0) + 1
        self._consecutive_lost[user_id] = n
        if n >= self._ZOMBIE_LOST_THRESHOLD:
            await self.force_reconnect(user_id)
            return True
        return False

    async def force_reconnect(self, user_id: int) -> None:
        """Tear down a user's stream so the next audio frame rebuilds it fresh.

        Zombie recovery: when the socket reports ``connected`` but delivers no
        transcripts, the passive (``connected is False``) reconnect never
        fires. This closes the stream and clears the reconnect throttle so the
        next inbound audio frame reconnects immediately (the pending-audio
        buffer preserves the recent frames). A deliberate heal — not throttled.
        """
        log.warning("Deepgram stream force-reconnect (zombie recovery)", user=user_id)
        await self.close_user(user_id)
        self._last_connect_time.pop(user_id, None)
        self._consecutive_lost[user_id] = 0

    async def keepalive(self, user_id: int) -> None:
        """Send a keepalive to prevent Deepgram from closing the WebSocket.

        Deepgram closes connections after ~10s of no audio with error 1011.
        The KeepAlive message resets this timer without sending audio data.
        Throttled to once per 5 seconds to avoid unnecessary traffic.
        """
        stream = self._streams.get(user_id)
        if stream is None or not stream.connected or stream.ws is None:
            return

        now = asyncio.get_event_loop().time()
        last_ka = getattr(stream, '_last_keepalive', 0.0)
        if now - last_ka < 5.0:
            return  # Throttle to once per 5 seconds

        try:
            import json as _json
            await stream.ws.send(_json.dumps({"type": "KeepAlive"}))
            stream._last_keepalive = now
        except Exception:
            stream.connected = False

    async def close_user(self, user_id: int) -> None:
        """Close a user's streaming connection."""
        stream = self._streams.pop(user_id, None)
        if stream and stream.ws:
            try:
                import json as _json
                await stream.ws.send(_json.dumps({"type": "CloseStream"}))
                await stream.ws.close()
            except Exception:
                pass
        if stream and stream.listener_task:
            stream.listener_task.cancel()

    async def cleanup(self) -> None:
        """Close all streaming connections and stop the keepalive loop.

        The keepalive task runs independently of streams; without cancelling
        it here it leaked a live task after every session (see
        docs/incidents/deepgram-streams-leak-on-cleanup.md).
        """
        if self._keepalive_task is not None and not self._keepalive_task.done():
            self._keepalive_task.cancel()
        self._keepalive_task = None
        for user_id in list(self._streams.keys()):
            await self.close_user(user_id)


class DualPipelineProcessor:
    """Orchestrates the dual-pipeline architecture for all users.

    Receives raw Discord audio per-user, downsamples, and feeds both
    Porcupine (wake word) and Deepgram (streaming STT) in parallel.

    When a complete utterance is detected (speech end + wake word active),
    fires the callback with the user ID and transcript.
    """

    def __init__(
        self,
        wake_word_model_path: str | None,
        deepgram_api_key: str,
        on_addressed_utterance: Callable[[int, str, str], None] | None = None,
        on_passive_utterance: Callable[[int, str, str], None] | None = None,
        bot_audio_active: Callable[[], bool] | None = None,
        deepgram_model: str = "nova-3",
    ) -> None:
        """Initialize the dual pipeline.

        Args:
            wake_word_model_path: Path to .onnx wake-word model file. ``None``
                falls back to OpenWakeWord's pre-trained ``hey_jarvis`` for
                testing. Production paths must live OUTSIDE ``/app/data/``
                (volume-mounted at runtime — see
                ``docs/gotchas/wake-word-model-path-conventions.md``).
            deepgram_api_key: Deepgram API key.
            on_addressed_utterance: Callback(user_id, user_name, transcript)
                when wake word + speech detected.
            on_passive_utterance: Callback(user_id, user_name, transcript)
                for passive context (no wake word).
            bot_audio_active: Callable that returns True when Poob is currently
                producing audio (TTS speaking, music playing). Used to decide
                whether a text-only wake match is trustworthy. When the bot is
                producing audio, mic loopback can cause Deepgram to hallucinate
                wake phrases — in that window we require BOTH acoustic and
                semantic confirmation. When the bot is silent, text-match alone
                is sufficient because openwakeword misses legitimate wakes in
                noisy environments (background TV/game audio, group calls).
            deepgram_model: Deepgram streaming model. Default "nova-3".
                Set to "flux-general-en" via DEEPGRAM_MODEL env var to try
                Deepgram Flux (Oct 2025, ~450ms P50 faster per benchmarks).
        """
        self._wake_detector = WakeWordDetector(
            model_path=wake_word_model_path or None,  # None = use pre-trained hey_jarvis
            threshold=0.7,  # Raised for multi-user — 0.5 causes false positives in group calls
        )
        self._deepgram = DeepgramStreamManager(
            api_key=deepgram_api_key, model=deepgram_model,
        )

        # Wake-word false-positive PCM capture (opt-in via env var).
        # On "Audio wake word overridden by text" events, dump a rolling
        # ~2s window of the user's 16kHz mono PCM to WAV for offline
        # wake-word retraining. Set WAKE_FP_CAPTURE_DIR=/app/data/wake_fp
        # in the Komodo env to enable. Each user gets a 100-frame ring.
        self._wake_fp_capture_dir = os.environ.get(
            "WAKE_FP_CAPTURE_DIR", "",
        ).strip()
        # 100 frames × 20ms = 2s at 16kHz mono 16-bit = ~64KB per user.
        self._wake_fp_buffers: dict[int, collections.deque[bytes]] = {}
        if self._wake_fp_capture_dir:
            try:
                os.makedirs(self._wake_fp_capture_dir, exist_ok=True)
                log.info(
                    "Wake-word FP capture enabled",
                    dir=self._wake_fp_capture_dir,
                )
            except OSError as exc:
                log.warning(
                    "Wake-word FP capture dir create failed",
                    dir=self._wake_fp_capture_dir, error=str(exc)[:80],
                )
                self._wake_fp_capture_dir = ""
        self._on_addressed = on_addressed_utterance
        self._on_passive = on_passive_utterance
        self._bot_audio_active = bot_audio_active or (lambda: False)
        self._user_pipelines: dict[int, UserPipeline] = {}
        self._silence_counters: dict[int, int] = {}
        # Serializes _emit_utterance across the two caller threads (recording
        # thread + the 100ms stale-buffer checker) — prevents a concurrent
        # read-emit race from double-emitting one utterance (2026-06-11).
        self._emit_lock = threading.Lock()
        # Per-user last addressed emission (normalized transcript, monotonic ts)
        # for utterance-level dedup — guards a re-triggered/resent identical
        # transcript from double-queuing a response. Complements the
        # music-layer dedup in PoobBrain (which only covers play actions).
        # See docs/plans/voice-pipeline-reliability.md.
        self._last_emitted: dict[int, tuple[str, float]] = {}
        # Differential silence thresholds based on whether the wake
        # word fired. Once the user has addressed Poob, they need
        # patience to articulate the full request — natural mid-sentence
        # pauses ("Hey, Poob. ... Play Can't Stop by ... Red Hot Chili
        # Peppers") run 1000-1800ms in real speech, and emitting at
        # 1000ms lopped the actual song name off into a passive
        # continuation. Passive utterances keep the tighter threshold
        # because they drive the rolling transcript, not a tool call.
        self._SILENCE_THRESHOLD_PASSIVE = 50   # 1000ms — fast rolling transcript
        self._SILENCE_THRESHOLD_ADDRESSED = 100  # 2000ms — let speakers finish
        # Backwards-compat alias used by tests / external callers that
        # still reference the old single-threshold attribute.
        self._SILENCE_THRESHOLD = self._SILENCE_THRESHOLD_PASSIVE
        self._loop: asyncio.AbstractEventLoop | None = None

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Set the asyncio event loop for scheduling async operations."""
        self._loop = loop
        self._deepgram.start_keepalive_loop(loop)

    def preconnect_user(self, user_id: int) -> None:
        """Pre-connect Deepgram WebSocket for a user immediately on join.

        Called as soon as audio frames start arriving — doesn't wait for
        speech detection. This ensures the transcript stream is ready
        before the user says their first word.
        """
        if self._loop and user_id not in self._deepgram._streams:
            asyncio.run_coroutine_threadsafe(
                self._deepgram.send_audio(user_id, b"\x00" * 640),  # 20ms silence to trigger connect
                self._loop,
            )

    # Regex for text-based wake word detection from Deepgram transcripts.
    # Covers phonetic variants STT engines produce for "Poob":
    # Poob, Pube, Pub, Poof, Poob, Boob, Hoob, Noob, etc.
    import re

    # Shared poob-variant stem. Deliberately the NARROW family (not the broad
    # address_detector._WAKE_WORDS set) — this gate feeds the anti-loopback
    # dual-gate, so widening the stem here re-opens the music/TTS-loopback hole
    # that gate was built to close. See docs/decisions/wake-word-dual-gate.md.
    _POOB_STEM = r'(?:p[ou]{1,2}b|p[ou]{1,2}be?|boob|hoob|noob|boop|poof|pub)'

    # Layer A — "hey poob" address. Requires the literal "hey" lead-in.
    _TEXT_WAKE_RE = re.compile(
        r'\bhey[\s,.]+' + _POOB_STEM + r'\b',
        re.IGNORECASE,
    )

    # Layer B — "command address". Catches real addresses where Deepgram
    # dropped/mangled the "hey" lead-in ("A Poob play X", "Apoob, remove
    # nightcore", "Poob, nightcore"). Two conditions must BOTH hold so this
    # stays tight enough not to fire on conversational mentions of "poob":
    #
    #   (a) a poob-variant opens the utterance — first word, tolerating one
    #       leading filler word (a/uh/um/oh/hey/ok/k) and/or a single glued
    #       filler letter ("apoob" = "a"+"poob"). Anchored at ^, so mid/end
    #       mentions ("...fucked up, Poob", "Get Poob out of") never qualify.
    #   (b) an imperative command verb appears anywhere, word-boundaried, so
    #       "playlist" / "PUBG" don't count as "play" / "pub".
    #
    # This is Option B from docs/incidents/wake-gate-stt-mishear-rejection.md.
    # The glued filler letter is restricted to single-letter filler words
    # (a, k), NOT any [a-z], so "spoof"/"scoob" can't be read as filler+stem.
    _POOB_OPENER_RE = re.compile(
        r'^[\s,.!?]*'
        r'(?:(?:a|uh+|um+|oh|hey|ok|okay|k)[\s,.!?]+)?'
        r'(?:a|k)?'
        + _POOB_STEM + r'\b',
        re.IGNORECASE,
    )
    _COMMAND_VERB_RE = re.compile(
        r'\b(?:play|queue|skip|next|stop|pause|resume|unpause|remove|clear|'
        r'cancel|volume|louder|quieter|lower|raise|mute|unmute|nightcore|slow|'
        r'slowed|speed|reverb|bass|shuffle|autoplay|kill|restart|replay|repeat|'
        r'turn)\b',
        re.IGNORECASE,
    )

    def _text_wake_word_match(self, transcript: str) -> bool:
        """Check if transcript is a text address to Poob.

        Layer 2 of wake word detection — catches what the audio model misses
        and feeds the dual-gate's ``text_match`` signal. Two recognition paths:

        - "hey poob" + phonetic variants (``_TEXT_WAKE_RE``).
        - "command address": a poob-variant opening the utterance plus an
          imperative verb, for when STT drops the "hey" lead-in. See
          docs/incidents/wake-gate-stt-mishear-rejection.md.

        Either path returning True is a match. The narrow stem and the
        opener+verb conjunction keep this from re-opening the loopback hole
        guarded by docs/decisions/wake-word-dual-gate.md.
        """
        if self._TEXT_WAKE_RE.search(transcript):
            log.info("Text wake word match", transcript=transcript[:60])
            return True
        if self._POOB_OPENER_RE.match(transcript) and self._COMMAND_VERB_RE.search(transcript):
            log.info("Command-address wake word match", transcript=transcript[:60])
            return True
        return False

    def _get_pipeline(self, user_id: int, user_name: str = "") -> UserPipeline:
        """Get or create per-user pipeline state."""
        if user_id not in self._user_pipelines:
            self._user_pipelines[user_id] = UserPipeline(
                user_id=user_id,
                user_name=user_name,
            )
        return self._user_pipelines[user_id]

    def process_audio_frame(
        self,
        user_id: int,
        pcm_stereo_48k: bytes,
        user_name: str = "",
    ) -> None:
        """Process a single Discord audio frame through both pipelines.

        Called from the voice thread for every 20ms PCM frame per user.
        Downsamples and feeds both Porcupine (wake word) and Deepgram
        (streaming STT) in parallel.

        Args:
            user_id: Discord user ID.
            pcm_stereo_48k: Raw 48kHz stereo 16-bit PCM (3840 bytes).
            user_name: Display name for logging.
        """
        pipeline = self._get_pipeline(user_id, user_name)
        pipeline.last_audio_time = time.monotonic()

        # Pre-connect Deepgram on first frame from any user — don't wait for speech
        if not getattr(pipeline, '_deepgram_preconnected', False):
            pipeline._deepgram_preconnected = True
            self.preconnect_user(user_id)

        # Downsample 48kHz stereo → 16kHz mono
        pcm_16k = downsample_48k_stereo_to_16k_mono(pcm_stereo_48k)
        if not pcm_16k:
            return

        # Rolling 2-second ring buffer per user for wake-FP training capture.
        # No-op when WAKE_FP_CAPTURE_DIR is unset (opt-in).
        if getattr(self, "_wake_fp_capture_dir", ""):
            buf = self._wake_fp_buffers.get(user_id)
            if buf is None:
                buf = collections.deque(maxlen=100)
                self._wake_fp_buffers[user_id] = buf
            buf.append(pcm_16k)

        # Check energy (simple VAD)
        samples = np.frombuffer(pcm_16k, dtype=np.int16)
        energy = float(np.sqrt(np.mean(samples.astype(np.float64) ** 2)))
        is_speech = energy >= 150.0

        # === ALWAYS feed wake word detector — even during silence ===
        # OpenWakeWord's mel spectrogram and embedding buffers need continuous
        # audio to maintain coherent internal state. Gaps in the audio stream
        # create feature discontinuities that kill detection on the next utterance.
        #
        # However, we ONLY accept detections when there's actual speech energy.
        # This prevents false positives from ambient noise / silence scoring
        # above threshold (the model sometimes fires on background noise).
        raw_hit = self._wake_detector.process_frame(user_id, pcm_16k)
        wake_hit = raw_hit and is_speech  # Gate on voice activity
        if wake_hit and not pipeline.is_active:
            # First detection for this utterance — log once and set state.
            # _pending_wake tracks hits before speech_started so the next
            # utterance inherits them. Decays after 3 seconds.
            pipeline._pending_wake = True
            pipeline._pending_wake_time = time.monotonic()
            if pipeline.speech_started:
                pipeline.is_active = True
                pipeline.wake_word_time = time.monotonic()
                # speech_to_wake_ms = time from utterance start to wake match,
                # NOT detection-processing latency (which is per-frame, sub-100ms).
                # A large value means the phrase landed late in a long/crosstalk
                # utterance — not a perf regression. See
                # docs/plans/voice-pipeline-reliability.md (Issue 1).
                log.info(
                    "Wake word detected",
                    user=user_name or user_id,
                    speech_to_wake_ms=int((time.monotonic() - pipeline.speech_start_time) * 1000),
                )

        # Handle new utterance start BEFORE sending to Deepgram.
        if is_speech and not pipeline.speech_started:
            pipeline.speech_started = True
            pipeline.speech_start_time = time.monotonic()
            # Inherit pending wake word if it fired recently (within 3s)
            pending = getattr(pipeline, '_pending_wake', False)
            pending_time = getattr(pipeline, '_pending_wake_time', 0.0)
            if pending and (time.monotonic() - pending_time) < 1.5:  # Tighter window — 3s caused stale carries
                pipeline.is_active = True
                pipeline._pending_wake = False
                log.info(
                    "Wake word carried from pre-speech detection",
                    user=user_name or user_id,
                )
            else:
                pipeline.is_active = wake_hit
                pipeline._pending_wake = False
                if wake_hit:
                    # Re-arm on the speech-start frame. Previously SILENT — the
                    # 2026-06-11 double-fire's silent re-address was invisible
                    # because only the branch above logged a wake. Always log.
                    log.info(
                        "Wake word detected",
                        user=user_name or user_id,
                        speech_to_wake_ms=int(
                            (time.monotonic() - pipeline.speech_start_time) * 1000
                        ),
                        rearm=True,
                    )
            pipeline.current_transcript = ""
            self._silence_counters[user_id] = 0
            self._deepgram.reset_transcript(user_id)
            # New utterance begun — bump the seal seq so this utterance's
            # transcript un-seals while the prior emitted one stays sealed
            # (the genuine-re-request discriminator). 2026-06-11.
            self._deepgram.begin_utterance(user_id)

        # Send audio to Deepgram during active speech.
        # Keepalive during silence is handled by the background keepalive loop.
        if (is_speech or pipeline.speech_started) and self._loop:
            asyncio.run_coroutine_threadsafe(
                self._deepgram.send_audio(user_id, pcm_16k),
                self._loop,
            )

        if is_speech:
            self._silence_counters[user_id] = 0
            # Force-emit if utterance exceeds max duration (15s).
            # In multi-user calls, cross-talk prevents silence detection,
            # causing utterances to accumulate for 30-60+ seconds.
            if pipeline.speech_started:
                duration = time.monotonic() - pipeline.speech_start_time
                if duration > 15.0:
                    self._emit_utterance(user_id, pipeline)
        else:
            # Silence frame
            if pipeline.speech_started:
                self._silence_counters[user_id] = self._silence_counters.get(user_id, 0) + 1

                # Patient threshold once the wake word has fired —
                # otherwise we cut off the addressed utterance halfway
                # through the request and the actual song / question
                # arrives 1-2 seconds later as a passive continuation.
                threshold = (
                    self._SILENCE_THRESHOLD_ADDRESSED
                    if pipeline.is_active
                    else self._SILENCE_THRESHOLD_PASSIVE
                )
                if self._silence_counters[user_id] >= threshold:
                    self._emit_utterance(user_id, pipeline)

    def _emit_utterance(self, user_id: int, pipeline: UserPipeline) -> None:
        """Serialize emission across the two caller threads (Pycord recording
        thread + the 100ms stale-buffer checker). The lock is held only across
        the cheap synchronous read-emit-reset (no awaits/IO), so it cannot
        deadlock; the per-utterance SEAL (GIL-atomic seq compare) handles the
        listener-thread interleave. See vc-session-failures-2026-06-11."""
        # getattr keeps it safe under __new__-constructed test instances that
        # skip __init__ (mirrors _last_emitted / _is_duplicate_emit).
        lock = getattr(self, "_emit_lock", None)
        if lock is None:
            lock = self._emit_lock = threading.Lock()
        with lock:
            self._emit_utterance_locked(user_id, pipeline)

    def _emit_utterance_locked(self, user_id: int, pipeline: UserPipeline) -> None:
        """Emit a complete utterance (speech ended).

        Grabs the streaming transcript from Deepgram (already built in
        real-time as user spoke) and fires the appropriate callback.

        If the transcript isn't ready yet (Deepgram async lag), schedules
        a deferred check instead of discarding the utterance.
        """
        pipeline.speech_started = False
        self._silence_counters[user_id] = 0

        # Get the transcript that Deepgram built while the user was speaking
        transcript, _is_final = self._deepgram.get_transcript(user_id)

        if not transcript:
            # Transcript not ready — Deepgram's async listener may not have
            # delivered it yet. Schedule a deferred check if wake word fired
            # (we MUST NOT discard an addressed utterance).
            if pipeline.is_active and self._loop:
                log.debug(
                    "Wake word active but transcript empty — deferring",
                    user=pipeline.user_name or user_id,
                )
                asyncio.run_coroutine_threadsafe(
                    self._deferred_emit(user_id, pipeline.user_name, pipeline.speech_start_time),
                    self._loop,
                )
                pipeline.is_active = False
                return
            # No wake word and no transcript — just discard
            log.debug("No streaming transcript, discarding passive utterance", user=user_id)
            pipeline.is_active = False
            return

        # Context-aware dual-gate wake word detection.
        #
        # Three real classes of utterance we need to discriminate:
        #   1. User says "Hey Poob" cleanly       → both audio + text fire
        #   2. User says "Hey Poob" in noise       → text fires, audio misses
        #                                             (openwakeword is brittle
        #                                             in group calls / game
        #                                             audio / background TV)
        #   3. Bot's own music "hey poob" lyric   → text fires, audio misses
        #      or TTS feedback via mic loopback     (Deepgram keyterm bias
        #                                             hallucinates the phrase
        #                                             from ambient loopback)
        #
        # Cases 1 and 2 are legitimate addresses; case 3 is the bug we hit
        # on April 21 (Armin van Buuren "Blah Blah Blah" → duplicate Toob).
        #
        # The discriminator between cases 2 and 3 is: **is the bot currently
        # producing audio?** Case 3 can only happen when the bot's own output
        # is feeding back through someone's mic. When the bot is silent,
        # loopback is impossible and text-alone is trustworthy.
        #
        # Rule:
        #   text_match and audio_match                   → addressed
        #   text_match and (bot silent)                  → addressed
        #   text_match and (bot producing audio) and not audio_match
        #                                                → reject (loopback)
        #   not text_match                               → reject
        #
        # This keeps us robust to both failure modes (openwakeword misses in
        # noise; Deepgram hallucinates during loopback) without overcorrecting.
        text_match = self._text_wake_word_match(transcript)
        audio_match = pipeline.is_active
        try:
            bot_audio = bool(self._bot_audio_active())
        except Exception:
            bot_audio = False  # Callable broken — fail open to text-only.

        if text_match and audio_match:
            is_addressed = True
        elif text_match and not bot_audio:
            # Bot is silent → no loopback risk → trust the transcript.
            is_addressed = True
        elif text_match and bot_audio and not audio_match:
            # Bot producing audio + text hit without acoustic confirmation →
            # high loopback probability, reject.
            is_addressed = False
            log.info(
                "Text wake word rejected (bot audio active, no acoustic confirmation)",
                user=pipeline.user_name or user_id,
                transcript=transcript[:80],
            )
        else:
            # No text match — audio-only fires are always rejected (openwakeword
            # hits on "hey" / coughs / laughs without real wake intent).
            is_addressed = False
            if audio_match:
                log.info(
                    "Audio wake word overridden by text (no match in transcript)",
                    user=pipeline.user_name or user_id,
                    transcript=transcript[:60],
                )
                self._dump_wake_fp(user_id, pipeline.user_name, transcript)

        self._do_emit(user_id, pipeline.user_name, pipeline.speech_start_time,
                      is_addressed, transcript)
        pipeline.is_active = False
        # Clear transcript immediately after emission to prevent replay
        self._deepgram.reset_transcript(user_id)

    async def _deferred_emit(
        self, user_id: int, user_name: str, speech_start_time: float
    ) -> None:
        """Wait briefly for Deepgram transcript, then emit addressed utterance."""
        # Give Deepgram up to 1.5s to deliver the transcript
        for _ in range(15):
            await asyncio.sleep(0.1)
            transcript, _is_final = self._deepgram.get_transcript(user_id)
            if transcript:
                self._deepgram.note_transcript_delivered(user_id)
                self._do_emit(user_id, user_name, speech_start_time, True, transcript)
                return
        # Still nothing — the stream may be a zombie (connected but mute).
        # Track the miss; force-reconnect after enough consecutive ones so the
        # user isn't silently deaf to Poob for the rest of the session.
        recovered = await self._deepgram.report_lost_transcript(user_id)
        log.warning(
            "Wake word fired but Deepgram never delivered transcript",
            user=user_name or user_id,
            zombie_recovery=recovered,
        )

    def _do_emit(
        self,
        user_id: int,
        user_name: str,
        speech_start_time: float,
        is_addressed: bool,
        transcript: str,
    ) -> None:
        """Actually emit an utterance to the appropriate callback."""
        duration_ms = (time.monotonic() - speech_start_time) * 1000
        log.info(
            "Utterance complete",
            user=user_name or user_id,
            duration_ms=int(duration_ms),
            wake_word=is_addressed,
            transcript=transcript[:80],
        )

        if is_addressed:
            if self._is_duplicate_emit(user_id, transcript):
                log.info(
                    "Duplicate addressed utterance suppressed",
                    user=user_name or user_id,
                    transcript=transcript[:60],
                )
                self._deepgram.reset_transcript(user_id)
                return
            if self._on_addressed:
                self._on_addressed(user_id, user_name, transcript)
        else:
            if self._on_passive:
                self._on_passive(user_id, user_name, transcript)

        # SEAL this utterance so Deepgram re-appends to the same (still-open)
        # utterance can't be re-emitted (2026-06-11 double-fire).
        self._deepgram.mark_emitted(user_id)
        self._deepgram.reset_transcript(user_id)

    _EMIT_DEDUP_WINDOW_S: float = 8.0

    def _is_duplicate_emit(self, user_id: int, transcript: str) -> bool:
        """True if this addressed transcript repeats the user's previous
        addressed emit within ``_EMIT_DEDUP_WINDOW_S``.

        Guards against the same utterance being emitted twice (rapid
        re-trigger or a Deepgram segment resend) double-queuing a
        response. Scoped to addressed emits only — passive transcripts
        feed rolling context where a repeat is harmless. ``getattr`` keeps
        it safe under ``__new__``-constructed test instances.
        """
        norm = " ".join(transcript.lower().split())
        if not norm:
            return False
        cache = getattr(self, "_last_emitted", None)
        if cache is None:
            cache = self._last_emitted = {}
        now = time.monotonic()
        prev = cache.get(user_id)
        cache[user_id] = (norm, now)
        if prev is None:
            return False
        prev_norm, prev_ts = prev
        if now - prev_ts > self._EMIT_DEDUP_WINDOW_S:
            return False
        return prev_norm == norm

    def check_stale_buffers(self) -> None:
        """Check for users who stopped sending audio (packet gap detection)."""
        now = time.monotonic()
        for user_id, pipeline in list(self._user_pipelines.items()):
            if not pipeline.speech_started:
                continue
            gap_ms = (now - pipeline.last_audio_time) * 1000
            if gap_ms > 1200:  # Match silence threshold (~1s) + buffer
                self._emit_utterance(user_id, pipeline)

    def _dump_wake_fp(
        self, user_id: int, user_name: str, transcript: str,
    ) -> None:
        """Serialize the per-user rolling PCM ring to a WAV file.

        Called on audio-wake-word-overridden-by-text events. The buffer
        holds ~2 seconds of 16kHz mono PCM ending at the point where
        the text gate overrode the acoustic hit. No-op if capture is
        disabled, the buffer is empty, or the processor was constructed
        via __new__ in tests (attribute not set).
        """
        capture_dir = getattr(self, "_wake_fp_capture_dir", "")
        if not capture_dir:
            return
        buffers = getattr(self, "_wake_fp_buffers", None)
        if not buffers:
            return
        buf = buffers.get(user_id)
        if not buf:
            return
        pcm_bytes = b"".join(buf)
        if not pcm_bytes:
            return
        ts = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
        safe_user = "".join(
            c for c in (user_name or str(user_id)) if c.isalnum() or c in "-_"
        )[:32] or str(user_id)
        filename = f"{ts}_{safe_user}_{user_id}.wav"
        path = os.path.join(capture_dir, filename)
        try:
            with wave.open(path, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)  # 16-bit
                wf.setframerate(16000)
                wf.writeframes(pcm_bytes)
        except (OSError, wave.Error) as exc:
            log.warning(
                "Wake-word FP capture write failed",
                path=path, error=str(exc)[:80],
            )
            return
        log.info(
            "Wake-word FP captured",
            path=path, transcript=transcript[:60],
            duration_ms=int(len(pcm_bytes) / 32),  # 2 bytes/sample × 16000 Hz
        )

    async def cleanup(self) -> None:
        """Release all resources."""
        self._wake_detector.cleanup()
        await self._deepgram.cleanup()
