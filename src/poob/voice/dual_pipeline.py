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
import struct
import time
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


class DeepgramStreamManager:
    """Manages per-user Deepgram streaming WebSocket connections.

    Each user gets a persistent WebSocket that receives audio chunks
    and returns interim/final transcripts in real-time. Transcripts
    build incrementally as the user speaks — by the time they finish,
    the full text is already available (or nearly so).

    Uses raw websockets for maximum control and reliability.
    """

    def __init__(self, api_key: str, model: str = "nova-3") -> None:
        self._api_key = api_key
        self._model = model
        self._streams: dict[int, _UserStream] = {}
        self._connect_locks: dict[int, asyncio.Lock] = {}
        self._last_connect_time: dict[int, float] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._keepalive_task: asyncio.Task | None = None

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
            # Need to connect/reconnect — use lock to prevent races
            if user_id not in self._connect_locks:
                self._connect_locks[user_id] = asyncio.Lock()

            lock = self._connect_locks[user_id]
            if lock.locked():
                return  # Another coroutine is already connecting — skip this frame

            async with lock:
                stream = self._streams.get(user_id)
                if stream is not None and stream.connected:
                    pass  # Connected while we waited for lock
                else:
                    # Throttle reconnects to once per 5 seconds
                    now = asyncio.get_event_loop().time()
                    last = self._last_connect_time.get(user_id, 0.0)
                    if now - last < 5.0:
                        return
                    self._last_connect_time[user_id] = now
                    if stream is not None:
                        log.info("Deepgram stream reconnecting", user=user_id)
                    stream = await self._connect_user(user_id)

        if not stream.connected or stream.ws is None:
            return

        try:
            await stream.ws.send(pcm_16k_mono)
        except Exception:
            stream.connected = False

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
        # Prefer finalized transcript, fall back to interim
        text = stream.transcript
        if not text and stream.latest_interim:
            text = stream.latest_interim
        return text, stream.is_final

    def reset_transcript(self, user_id: int) -> None:
        """Clear transcript state for a user (after processing)."""
        stream = self._streams.get(user_id)
        if stream:
            stream.transcript = ""
            stream.latest_interim = ""
            stream.is_final = False

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
        """Close all streaming connections."""
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
        porcupine_access_key: str,
        porcupine_keyword_path: str | None,
        deepgram_api_key: str,
        on_addressed_utterance: Callable[[int, str, str], None] | None = None,
        on_passive_utterance: Callable[[int, str, str], None] | None = None,
        bot_audio_active: Callable[[], bool] | None = None,
    ) -> None:
        """Initialize the dual pipeline.

        Args:
            porcupine_access_key: Picovoice access key.
            porcupine_keyword_path: Path to custom .ppn model file.
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
        """
        self._wake_detector = WakeWordDetector(
            model_path=porcupine_keyword_path or None,  # None = use pre-trained hey_jarvis
            threshold=0.7,  # Raised for multi-user — 0.5 causes false positives in group calls
        )
        self._deepgram = DeepgramStreamManager(api_key=deepgram_api_key)
        self._on_addressed = on_addressed_utterance
        self._on_passive = on_passive_utterance
        self._bot_audio_active = bot_audio_active or (lambda: False)
        self._user_pipelines: dict[int, UserPipeline] = {}
        self._silence_counters: dict[int, int] = {}
        self._SILENCE_THRESHOLD = 50  # 50 frames × 20ms = 1000ms silence → end of speech
        # 1000ms is the sweet spot: long enough to not split natural pauses
        # ("Hey Jarvis, [pause] what do you think?") but short enough to feel
        # responsive. Matches Deepgram's utterance_end_ms=1500 reasonably.
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
    _TEXT_WAKE_RE = re.compile(
        r'\bhey[\s,.]+'
        r'(?:p[ou]{1,2}b|p[ou]{1,2}be?|boob|hoob|noob|boop|poof|pub)\b',
        re.IGNORECASE,
    )

    def _text_wake_word_match(self, transcript: str) -> bool:
        """Check if transcript contains 'Hey Poob' or phonetic variants.

        This is Layer 2 of wake word detection — catches what the audio
        model misses. Deepgram's keyterm=Poob helps but STT still produces
        variants like 'Hey Pube', 'Hey Pub', 'Hey Boob', etc.
        """
        if self._TEXT_WAKE_RE.search(transcript):
            log.info("Text wake word match", transcript=transcript[:60])
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
                log.info(
                    "Wake word detected",
                    user=user_name or user_id,
                    latency_ms=int((time.monotonic() - pipeline.speech_start_time) * 1000),
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
            pipeline.current_transcript = ""
            self._silence_counters[user_id] = 0
            self._deepgram.reset_transcript(user_id)

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

                if self._silence_counters[user_id] >= self._SILENCE_THRESHOLD:
                    self._emit_utterance(user_id, pipeline)

    def _emit_utterance(self, user_id: int, pipeline: UserPipeline) -> None:
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
                self._do_emit(user_id, user_name, speech_start_time, True, transcript)
                return
        # Still nothing — log it so we know
        log.warning(
            "Wake word fired but Deepgram never delivered transcript",
            user=user_name or user_id,
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
            if self._on_addressed:
                self._on_addressed(user_id, user_name, transcript)
        else:
            if self._on_passive:
                self._on_passive(user_id, user_name, transcript)

        self._deepgram.reset_transcript(user_id)

    def check_stale_buffers(self) -> None:
        """Check for users who stopped sending audio (packet gap detection)."""
        now = time.monotonic()
        for user_id, pipeline in list(self._user_pipelines.items()):
            if not pipeline.speech_started:
                continue
            gap_ms = (now - pipeline.last_audio_time) * 1000
            if gap_ms > 1200:  # Match silence threshold (~1s) + buffer
                self._emit_utterance(user_id, pipeline)

    async def cleanup(self) -> None:
        """Release all resources."""
        self._wake_detector.cleanup()
        await self._deepgram.cleanup()
