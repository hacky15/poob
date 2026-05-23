"""Voice session orchestrator — ties STT, LLM, and TTS together.

Manages the full pipeline: audio utterance → transcription → LLM response
→ speech synthesis → Discord playback. Handles interrupt (user speaks while
bot is talking), queuing, and thread-safe bridging between Discord's voice
thread and the asyncio event loop.
"""

from __future__ import annotations

import asyncio
import glob
import os
import shutil
import subprocess
from typing import Any, TYPE_CHECKING

import discord


def _find_ffmpeg() -> str:
    """Find the ffmpeg executable, checking common install locations on Windows.

    Returns:
        Full path to ffmpeg, or 'ffmpeg' if not found (will rely on PATH).
    """
    # Check PATH first
    found = shutil.which("ffmpeg")
    if found:
        return found

    # Check WinGet install location (where winget install Gyan.FFmpeg puts it)
    winget_pattern = os.path.expanduser(
        "~/AppData/Local/Microsoft/WinGet/Packages/Gyan.FFmpeg*/ffmpeg-*-full_build/bin/ffmpeg.exe"
    )
    matches = glob.glob(winget_pattern)
    if matches:
        return matches[0]

    # Check common locations
    for candidate in [
        r"C:\ffmpeg\bin\ffmpeg.exe",
        r"C:\ProgramData\chocolatey\bin\ffmpeg.exe",
        r"C:\tools\ffmpeg\bin\ffmpeg.exe",
    ]:
        if os.path.isfile(candidate):
            return candidate

    return "ffmpeg"  # Fall back to PATH lookup


FFMPEG_PATH = _find_ffmpeg()


_TOOB_FILTER_CHAIN = (
    "asetrate=18500,aresample=24000,"
    "atempo=2.0,"
    "vibrato=f=5.5:d=0.15,"
    "bass=g=6:f=80,"
    "aecho=0.8:0.85:40:0.3,"
    "volume=1.35"
)

# Boob — Toob's side piece. Inverse of the warlord chain: pitch UP a touch,
# slightly faster, no echo, no bass boost. The result is a friendly, slightly
# squeaky, intimate voice that reads as the warm opposite of Toob's cavernous
# menace. See decisions/boob-music-wrap-variant for stage rationale.
#   asetrate=28000 on a 24kHz source → +2.7 semitones, ~17% faster duration
#   aresample=24000 — restore the playable rate after the pitch shift
#   vibrato f=6.5,d=0.10 — slightly brighter wobble than Toob, half the depth
#   volume=1.1 — light pre-gain; speechnorm absorbs the rest downstream
_BOOB_FILTER_CHAIN = (
    "asetrate=28000,aresample=24000,"
    "vibrato=f=6.5:d=0.10,"
    "volume=1.1"
)


def _prewarm_ffmpeg() -> None:
    """Prime FFmpeg's binary + filter-graph init caches at import.

    Toob's first filter-chain invocation cold-starts at ~770ms on a
    fresh container — most of it is FFmpeg binary load + filter graph
    construction. We do both up front:

    1. `-version` call — pulls the binary into the OS page cache.
    2. A 0.3s silence through the real Toob filter chain — primes the
       filter-graph initialization path and exposes any compatibility
       issues before first production use.

    Synchronous (~150-400ms total). Runs once per container lifetime.
    Failures are swallowed — prewarm is an optimization, not a
    correctness requirement.
    """
    try:
        subprocess.run(
            [FFMPEG_PATH, "-hide_banner", "-version"],
            capture_output=True, timeout=2.0,
        )
    except (subprocess.SubprocessError, OSError):
        return
    try:
        subprocess.run(
            [
                FFMPEG_PATH, "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono",
                "-t", "0.3",
                "-af", _TOOB_FILTER_CHAIN,
                "-f", "null", "-",
            ],
            capture_output=True, timeout=3.0,
        )
    except (subprocess.SubprocessError, OSError):
        pass
    # Boob filter graph — different shape from Toob's, needs its own warm-up
    # so the rare ~1-in-20 hit doesn't pay a 700ms cold start the one time
    # someone actually triggers it.
    try:
        subprocess.run(
            [
                FFMPEG_PATH, "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono",
                "-t", "0.3",
                "-af", _BOOB_FILTER_CHAIN,
                "-f", "null", "-",
            ],
            capture_output=True, timeout=3.0,
        )
    except (subprocess.SubprocessError, OSError):
        pass

from poob.utils.logging import get_logger
from poob.voice.audio_buffer import (
    BYTES_PER_FRAME,
    FRAME_DURATION_MS,
    UserAudioBuffer,
    VADConfig,
)
from poob.voice.address_detector import MultiSignalAddressDetector
from poob.voice.fillers import FillerPlayer
from poob.voice.silero_vad import (
    DISCORD_FRAME_BYTES,
    DISCORD_FRAME_MS,
    SileroVADConfig,
    SileroVADProcessor,
    SpeechDetector,
)
from poob.voice.stt import STTProvider
from poob.voice.tts import TTSProvider

if TYPE_CHECKING:
    from poob.brain.poob import PoobBrain

log = get_logger("voice.session")
log.info("ffmpeg resolved", path=FFMPEG_PATH)
_prewarm_ffmpeg()
log.info("ffmpeg prewarmed")


class VoiceSession:
    """Manages a single voice channel session.

    One VoiceSession per guild/channel the bot is connected to.
    Handles multiple users speaking (per-user buffers), processes
    utterances sequentially, and plays responses back.

    Args:
        voice_client: Discord voice client (connected to a channel).
        stt_providers: Ordered list of STT providers (try first, fallback).
        tts_providers: Ordered list of TTS providers.
        brain: Unified PoobBrain — handles personality, casual chat, and
            deal routing for voice interactions.
        vad_config: Voice activity detection config.
        loop: The asyncio event loop (for thread-safe scheduling).
    """

    def __init__(
        self,
        voice_client: discord.VoiceClient,
        stt_providers: list[STTProvider],
        tts_providers: list[TTSProvider],
        brain: PoobBrain,
        vad_config: VADConfig | None = None,
        loop: asyncio.AbstractEventLoop | None = None,
        dual_pipeline_config: dict | None = None,
    ) -> None:
        self.voice_client = voice_client
        self.stt_providers = stt_providers
        self.tts_providers = tts_providers
        self.brain = brain
        self.vad_config = vad_config or VADConfig()
        self._loop = loop or asyncio.get_event_loop()
        # Per-guild isolation: every brain call we make passes this id
        # so PoobBrain can keep histories, dedup state, and music-info
        # scoped to this guild only. Captured once on session init —
        # voice_client.guild is stable for the session lifetime.
        guild = getattr(voice_client, "guild", None)
        self._guild_id = int(getattr(guild, "id", 0) or 0)

        # --- Dual Pipeline (OpenWakeWord + Deepgram streaming) ---
        # When configured, replaces the old batch STT pipeline with:
        # 1. OpenWakeWord local .onnx wake word detection (~50ms, raw audio)
        # 2. Deepgram streaming STT (transcript ready when speech ends)
        self._dual_pipeline = None
        if dual_pipeline_config:
            dg_key = dual_pipeline_config.get("deepgram_api_key", "")
            model_path = dual_pipeline_config.get("wake_word_model_path", "")
            dg_model = dual_pipeline_config.get("deepgram_model", "nova-3")
            if dg_key:
                from poob.voice.dual_pipeline import DualPipelineProcessor
                self._dual_pipeline = DualPipelineProcessor(
                    wake_word_model_path=model_path or None,
                    deepgram_api_key=dg_key,
                    on_addressed_utterance=self._on_dual_addressed,
                    on_passive_utterance=self._on_dual_passive,
                    bot_audio_active=self._bot_audio_active,
                    deepgram_model=dg_model,
                )
                self._dual_pipeline.set_loop(self._loop)
                log.info(
                    "Dual pipeline enabled (OpenWakeWord + Deepgram streaming)",
                    wake_model=os.path.basename(model_path) if model_path else "hey_jarvis (testing)",
                )

        # Silero VAD disabled — creates per-user model instances that are too slow
        # to initialize in multi-user channels. Energy-based VAD with packet gap
        # detection is reliable. Silero can be re-enabled once we solve:
        # 1. Shared model instance with per-user state management
        # 2. Lazy initialization (not on every new user join)
        self._silero_vad = None
        self._use_silero = False

        self._user_buffers: dict[int, UserAudioBuffer] = {}
        self._speech_detectors: dict[int, SpeechDetector] = {}
        self._is_speaking = False
        self._listening = True
        self._pending_utterances: list[tuple[int, bytes]] = []
        self.is_stage: bool = False
        # Music player reference — set by MusicCog when music starts.
        # When set, TTS is injected as overlay instead of replacing audio.
        self.music_player: Any = None  # GuildMusicPlayer (avoid circular import)
        # Pending utterance counter — caps fire-and-forget tasks to prevent
        # API rate limit exhaustion. Replaces removed asyncio.Queue guard.
        self._pending_utterance_count = 0
        self.filler_player: FillerPlayer = FillerPlayer()
        # Response lock — only one LLM→TTS→play pipeline at a time
        # STT runs in parallel (fire-and-forget), but responses serialize
        self._response_lock = asyncio.Lock()
        # Addressed utterance queue — requests that arrive while Poob is
        # already responding get queued instead of dropped. Processed FIFO.
        self._addressed_queue: asyncio.Queue[tuple[int, str, str]] = asyncio.Queue(maxsize=5)
        # In-flight response tasks. Tracked so cleanup() can cancel
        # pending work when the bot leaves a voice channel.
        self._inflight_tasks: set[asyncio.Task] = set()

        # --- Passive context + multi-signal address detection ---
        # Rolling transcript of recent conversation (all users, attributed).
        self._transcript: list[dict] = []  # [{user_id, name, text, time}]
        self._max_transcript = 25  # Keep last 25 messages
        # User ID → display name cache
        self._user_names: dict[int, str] = {}
        # Multi-signal fusion address detector (research-backed)
        self._address_detector = MultiSignalAddressDetector()

    def _bot_audio_active(self) -> bool:
        """True when Poob's OWN VOICE is playing and could loop back through
        a user's mic as a hallucinated wake word.

        Scope narrowed April 22 2026: previously this also returned True when
        the music_player or voice_client was playing. That was wrong — the
        loopback class of bug is specifically Poob's TTS ("Hey there!")
        being re-transcribed via a listener's speakers; music tracks
        contain no wake-word phonemes and aren't a loopback risk. Treating
        music as bot-audio-active caused legitimate wake words ("Hey Poob,
        play the untold...") to be rejected during music playback — the
        exact failure mode Ben reported with Secession Studios.

        `_is_speaking` covers both standalone TTS and music-overlay TTS paths
        (set in `_play_audio` around the inject_tts_overlay/vc.play calls).
        """
        return self._is_speaking

    async def play_entrance(self) -> None:
        """Synthesize and play Poob's entrance catchphrase."""
        catchphrase = "Its poob here, auuuuuughhhhh yeahhhhhhhh"
        try:
            audio = await self._synthesize(catchphrase)
            if audio:
                await self._play_audio(audio)
                log.info("Entrance catchphrase played")
        except Exception as exc:
            log.warning("Entrance catchphrase failed", error=str(exc)[:80])

    def _resolve_user_name(self, user_id: int) -> str:
        """Resolve a Discord user ID to their display name.

        Checks channel members first, then guild cache. Caches results.
        """
        if user_id == 0:
            return "Poob"
        if user_id in self._user_names:
            return self._user_names[user_id]

        # Try channel members
        channel = self.voice_client.channel
        if channel:
            for member in channel.members:
                if member.id == user_id:
                    name = f"{member.display_name} ({member.name})"
                    self._user_names[user_id] = name
                    return name

        # Try guild member cache
        if channel and hasattr(channel, "guild") and channel.guild:
            member = channel.guild.get_member(user_id)
            if member:
                name = f"{member.display_name} ({member.name})"
                self._user_names[user_id] = name
                return name

        # Try voice states (Pycord populates these from VOICE_STATE_UPDATE)
        if channel and hasattr(channel, "voice_states"):
            for vs in channel.voice_states:
                if hasattr(vs, "id") and vs.id == user_id:
                    name = getattr(vs, "display_name", None) or getattr(vs, "name", None)
                    if name:
                        full = f"{name} ({getattr(vs, 'name', '?')})"
                        self._user_names[user_id] = full
                        return full

        # Try the bot's user cache directly
        if hasattr(self.voice_client, "client"):
            user = self.voice_client.client.get_user(user_id)
            if user:
                name = f"{user.display_name} ({user.name})"
                self._user_names[user_id] = name
                return name

        # Don't cache fallback — retry next time
        return f"User-{user_id}"

    def _add_to_transcript(self, user_id: int, text: str) -> None:
        """Add an attributed message to the rolling transcript."""
        import time as _time
        name = self._resolve_user_name(user_id)
        self._transcript.append({
            "user_id": user_id,
            "name": name,
            "text": text,
            "time": _time.monotonic(),
        })
        if len(self._transcript) > self._max_transcript:
            self._transcript = self._transcript[-self._max_transcript:]

    def _build_context(self) -> str:
        """Build an attributed conversation transcript for the LLM.

        Returns lines like:
            Ben (hacky15): yeah thats what I was saying
            Simon (holyhhaze21): Okay I see we are on the same page
            Poob: bro what are you guys even talking about
        """
        if not self._transcript:
            return ""
        lines = []
        for entry in self._transcript[-15:]:
            lines.append(f"{entry['name']}: {entry['text']}")
        return "\n".join(lines)

    # --- Dual Pipeline Callbacks ---
    # These are called from the DualPipelineProcessor when it detects
    # addressed (wake word) or passive (no wake word) utterances.
    # The transcript is ALREADY READY (built by Deepgram streaming).

    def _on_dual_addressed(self, user_id: int, user_name: str, transcript: str) -> None:
        """Called when wake word detected + speech ended. Transcript is ready."""
        import time as _time

        log.info(
            "Dual: wake word addressed",
            user=user_name,
            text=transcript[:100],
        )

        # Add to passive context
        self._add_to_transcript(user_id, transcript)
        self._address_detector.mark_human_spoke(addressed_bot=True)

        if hasattr(self, "_last_utterance_time"):
            self._last_utterance_time = _time.monotonic()

        # Build prompt and respond — transcript already available, no STT needed.
        # Track the task so cleanup() can cancel it on disconnect.
        def _spawn() -> None:
            task = self._loop.create_task(
                self._respond_to_transcript(user_id, user_name, transcript)
            )
            self._inflight_tasks.add(task)
            task.add_done_callback(self._inflight_tasks.discard)

        self._loop.call_soon_threadsafe(_spawn)

    def _on_dual_passive(self, user_id: int, user_name: str, transcript: str) -> None:
        """Called for non-wake-word utterances. Just add to passive context."""
        import time as _time

        log.info(
            "Dual: passive heard",
            user=user_name,
            text=transcript[:60],
        )
        self._add_to_transcript(user_id, transcript)
        self._address_detector.mark_human_spoke(addressed_bot=False)

        if hasattr(self, "_last_utterance_time"):
            self._last_utterance_time = _time.monotonic()

    async def _respond_to_transcript(self, user_id: int, user_name: str, transcript: str) -> None:
        """Generate and play a response to a transcribed utterance.

        Called when the dual pipeline detects an addressed utterance.
        If Poob is already responding to someone, the request is queued
        and processed after the current response finishes (FIFO).
        """
        import time as _time

        if self._response_lock.locked():
            # Queue instead of drop — will be processed after current response
            try:
                self._addressed_queue.put_nowait((user_id, user_name, transcript))
                log.info("Queued response (Poob busy)", user=user_name, queue_size=self._addressed_queue.qsize())
            except asyncio.QueueFull:
                log.warning("Response queue full, dropping", user=user_name)
            return

        # Process this request, then drain the queue
        await self._process_single_response(user_id, user_name, transcript)

        # Process any queued requests that arrived while we were responding
        while not self._addressed_queue.empty():
            try:
                q_uid, q_name, q_text = self._addressed_queue.get_nowait()
                await self._process_single_response(q_uid, q_name, q_text)
            except asyncio.QueueEmpty:
                break

    async def _process_single_response(self, user_id: int, user_name: str, transcript: str) -> None:
        """Process a single addressed utterance: LLM → TTS → play.

        Focused listen: only this user's request is processed. Other users'
        speech continues to be logged passively but doesn't interfere.
        """
        import time as _time
        t0 = _time.monotonic()

        async with self._response_lock:
            # Build context prompt. The CURRENT SPEAKER must be unambiguous,
            # but the addressee rule lives in the system prompt now — the
            # earlier user-message version was leaking into tool_call query
            # arguments (e.g. play tool got query="low by\n(If you address
            # them by name...)"). Trailing line is the transcript so the
            # LLM treats it as the user intent.
            conv_context = self._build_context()
            if conv_context:
                prompt = (
                    f"[Recent conversation you've been listening to:\n{conv_context}]\n\n"
                    f"=== The user speaking to you RIGHT NOW is {user_name} ===\n"
                    f"{user_name} just said to you: {transcript}"
                )
            else:
                prompt = f"{user_name} said to you: {transcript}"

            # Update brain's music context for THIS guild only so the
            # LLM knows if music is playing here (other guilds' music
            # state stays in their own slots).
            if self.music_player and self.music_player.current_track:
                track = self.music_player.current_track
                self.brain._set_music_playing_info(
                    self._guild_id,
                    f"{track.title} [{track.duration_str}]",
                )
            else:
                self.brain._set_music_playing_info(self._guild_id, "")

            # Generate response
            full_response = ""
            first_sentence = True
            self._is_speaking = True

            try:
                # Stream sentences from the brain and synth+play each one
                # as it arrives — don't collect the full response first.
                # Speculative-wrap music paths depend on this: the brain
                # yields a wrap sentence early while ytdl search is still
                # running; synth must start on that first yield, not wait
                # for the rest of the generator to finish.
                from poob.brain.poob import VOICE_BOOB, VOICE_TOOB
                voice_persona = "poob"
                synth_dispatch = {
                    "poob": self._synthesize,
                    "toob": self._synthesize_toob,
                    "boob": self._synthesize_boob,
                }
                async for item in self.brain.respond_streaming(
                    prompt, str(user_id), guild_id=self._guild_id,
                ):
                    # Voice signals — not text, just routing control. The
                    # brain yields exactly one of these as its first item
                    # when a non-default persona should speak.
                    if item == VOICE_TOOB:
                        voice_persona = "toob"
                        continue
                    if item == VOICE_BOOB:
                        voice_persona = "boob"
                        continue

                    full_response += item + " "

                    if first_sentence:
                        t_llm = _time.monotonic()
                        log.info(
                            "First sentence ready",
                            llm_ms=int((t_llm - t0) * 1000),
                            sentence=item[:60],
                            voice=voice_persona,
                        )
                        first_sentence = False

                    synth = synth_dispatch[voice_persona]
                    audio = await synth(item)
                    if not audio:
                        continue

                    # When music is playing, go straight to _play_audio()
                    # which handles TTS overlay (music ducks automatically).
                    # Only wait for previous TTS to finish, not music.
                    music_active = (
                        self.music_player is not None
                        and self.music_player.mixer is not None
                        and self.music_player.is_playing
                    )
                    if not music_active:
                        while self.voice_client.is_playing():
                            await asyncio.sleep(0.02)
                    await self._play_audio(audio)

                # Update state
                self._address_detector.mark_bot_spoke(full_response.strip())
                if full_response.strip():
                    self._add_to_transcript(0, full_response.strip()[:200])

                t_done = _time.monotonic()
                log.info(
                    "Response complete",
                    user=user_name,
                    response=full_response.strip()[:100],
                    total_ms=int((t_done - t0) * 1000),
                )

                # Session-level music orchestration: if the music player has
                # queued tracks but isn't playing, TTS just finished and we
                # should start music now. No flags — pure state inspection.
                if (
                    self.music_player is not None
                    and not self.music_player.is_playing
                    and not self.music_player.queue.is_empty
                ):
                    # Wait for last TTS audio to finish
                    while self.voice_client.is_playing():
                        await asyncio.sleep(0.05)
                    self.music_player.start_deferred()
                    log.info("Music started after TTS (deferred)")

            except Exception as exc:
                log.error("Response failed", user=user_name, error=str(exc)[:150])
            finally:
                self._is_speaking = False

    @property
    def is_listening(self) -> bool:
        return self._listening

    def toggle_listening(self) -> bool:
        """Toggle voice listening on/off. Returns new state."""
        self._listening = not self._listening
        log.info("Voice listening toggled", listening=self._listening)
        return self._listening

    @property
    def uses_dual_pipeline(self) -> bool:
        """Whether the dual pipeline (Porcupine + Deepgram) is active."""
        return self._dual_pipeline is not None

    def process_audio_frame(self, user_id: int, pcm_data: bytes) -> None:
        """Route an audio frame to the appropriate pipeline.

        If dual pipeline is active, feeds both Porcupine (wake word)
        and Deepgram (streaming STT) in parallel. Otherwise falls back
        to the old energy-based VAD + batch STT approach.

        Args:
            user_id: Discord user ID.
            pcm_data: Raw PCM bytes (48kHz stereo from Discord).
        """
        if self._dual_pipeline:
            user_name = self._resolve_user_name(user_id)
            self._dual_pipeline.process_audio_frame(user_id, pcm_data, user_name)
        else:
            # Legacy path: energy-based VAD → batch STT
            buffer = self.get_or_create_buffer(user_id)
            buffer.add_frame(pcm_data)

    def get_or_create_buffer(self, user_id: int) -> UserAudioBuffer | SpeechDetector:
        """Get or create an audio buffer/detector for a user (legacy path).

        Only used when dual pipeline is not active.
        """
        if self._use_silero:
            if user_id not in self._speech_detectors:
                self._speech_detectors[user_id] = SpeechDetector(
                    user_id=user_id,
                    on_utterance=self._on_utterance_detected,
                )
            return self._speech_detectors[user_id]

        if user_id not in self._user_buffers:
            self._user_buffers[user_id] = UserAudioBuffer(
                user_id=user_id,
                config=self.vad_config,
                on_utterance=self._on_utterance_detected,
            )
        return self._user_buffers[user_id]

    def _on_utterance_detected(self, user_id: int, pcm_audio: bytes) -> None:
        """Callback from VAD when a complete utterance is detected.

        Called from Discord's voice thread — must be thread-safe.

        ALWAYS transcribes into passive context. Only responds if addressed.
        If bot is busy speaking, the utterance still gets transcribed (for
        context) but Poob won't try to respond until he's done talking.
        """
        if not self._listening:
            return

        import time as _time
        self._last_utterance_time = _time.monotonic()

        duration_ms = len(pcm_audio) / DISCORD_FRAME_BYTES * DISCORD_FRAME_MS
        if duration_ms < 300:
            return  # Too short — noise/breath

        # Drop if too many utterances pending — prevents API rate limit exhaustion
        if self._pending_utterance_count > 4:
            return

        log.info(
            "Utterance detected, scheduling processing",
            user=user_id,
            audio_bytes=len(pcm_audio),
        )

        # Enqueue for sequential processing (thread-safe)
        self._loop.call_soon_threadsafe(
            self._enqueue_utterance, user_id, pcm_audio
        )

    def _enqueue_utterance(self, user_id: int, pcm_audio: bytes) -> None:
        """Fire-and-forget: start processing immediately as a new task.

        No queue. Each utterance gets its own task. If the bot is already
        responding, the task will detect that and just add to passive context.
        This eliminates the sequential bottleneck where one slow STT blocks
        everyone else.
        """
        self._pending_utterance_count += 1
        self._loop.create_task(self._process_utterance(user_id, pcm_audio))

    async def _process_utterance(self, user_id: int, pcm_audio: bytes) -> None:
        """Full pipeline: STT → passive context → address check → LLM → TTS.

        EVERY utterance is transcribed and added to the attributed transcript.
        Address detection determines if Poob should respond — using wake words,
        reply patterns, or an LLM classifier for ambiguous cases.
        When responding, the full conversation context is included.
        """
        try:
            import time as _time

            t0 = _time.monotonic()
            speaker_name = self._resolve_user_name(user_id)

            # 1. Speech-to-text with timeout (always — needed for passive context)
            try:
                text = await asyncio.wait_for(self._transcribe(pcm_audio), timeout=5.0)
            except asyncio.TimeoutError:
                log.warning("STT timeout (5s)", user=user_id, audio_bytes=len(pcm_audio))
                return
            except Exception as stt_err:
                log.error("STT failed", user=user_id, error=str(stt_err)[:100])
                return
            if not text or len(text.strip()) < 2:
                log.info("Empty STT", user=speaker_name, audio_bytes=len(pcm_audio))
                return

            t_stt = _time.monotonic()
            log.info(
                "STT result",
                user=speaker_name,
                text=text[:80],
                stt_ms=int((t_stt - t0) * 1000),
            )

            # 2. Add to attributed transcript (passive listening — always)
            self._add_to_transcript(user_id, text)

            if hasattr(self, "_last_utterance_time"):
                self._last_utterance_time = _time.monotonic()

            # 2b. If bot is currently speaking, just add to context and return.
            if self._is_speaking or self.voice_client.is_playing():
                log.info("Heard while speaking", user=speaker_name, text=text[:60])
                self._address_detector.mark_human_spoke(addressed_bot=False)
                return

            # 2c. Update member names for negative signal detection
            channel = self.voice_client.channel
            if channel:
                member_names = set()
                for m in channel.members:
                    if not m.bot:
                        member_names.add(m.display_name.lower())
                        member_names.add(m.name.lower())
                self._address_detector.update_member_names(member_names)

            # 3. Multi-signal fusion address detection
            addressed, score, signals = self._address_detector.should_respond(
                text=text,
            )

            if not addressed:
                log.info(
                    "Heard (passive)",
                    user=speaker_name,
                    text=text[:60],
                    score=f"{score:.2f}",
                )
                self._address_detector.mark_human_spoke(addressed_bot=False)
                return

            # Log which signals triggered the response
            active_signals = {k: f"{v:.2f}" for k, v in signals.items() if v > 0.01}
            log.info(
                "Addressed by user",
                user=speaker_name,
                text=text[:100],
                stt_ms=int((t_stt - t0) * 1000),
                score=f"{score:.2f}",
                signals=active_signals,
            )

            # 4. Acquire response lock — only one response at a time
            # If another response is already playing, skip (we're passive)
            if self._response_lock.locked():
                log.info("Skipping response (another playing)", user=speaker_name)
                self._address_detector.mark_human_spoke(addressed_bot=True)
                return

            async with self._response_lock:
                # Build context-enriched prompt with attributed messages
                conv_context = self._build_context()
                if conv_context:
                    prompt = (
                        f"[Recent conversation you've been listening to:\n{conv_context}]\n\n"
                        f"{speaker_name} said to you: {text}"
                    )
                else:
                    prompt = f"{speaker_name}: {text}"

                # 5. Generate response with sentence streaming.
                # guild_id from session init isolates per-guild state.
                full_response = ""
                first_sentence = True
                self._is_speaking = True

                async for sentence in self.brain.respond_streaming(
                    prompt, str(user_id), guild_id=self._guild_id,
                ):
                    full_response += sentence + " "

                    if first_sentence:
                        t_llm = _time.monotonic()
                        log.info(
                            "First sentence ready",
                            llm_ms=int((t_llm - t_stt) * 1000),
                            sentence=sentence[:60],
                        )
                        first_sentence = False

                    # 6. Synthesize and play each sentence
                    audio = await self._synthesize(sentence)
                    if audio:
                        while self.voice_client.is_playing():
                            await asyncio.sleep(0.05)
                        await self._play_audio(audio)

                # Update address detector state — Poob spoke
                self._address_detector.mark_bot_spoke(full_response.strip())

                # Add Poob's response to transcript for continuity
                if full_response.strip():
                    self._add_to_transcript(0, full_response.strip()[:200])

            t_done = _time.monotonic()
            log.info(
                "Response complete",
                user=speaker_name,
                response=full_response.strip()[:100],
                total_ms=int((t_done - t0) * 1000),
            )

            # Discard queued utterances — they're already in the transcript
            if self._pending_utterances:
                self._pending_utterances.clear()

        except asyncio.CancelledError:
            log.debug("Processing cancelled", user=user_id)
        except Exception as exc:
            log.error("Processing failed", user=user_id, error=str(exc)[:150])
        finally:
            self._is_speaking = False
            self._pending_utterance_count = max(0, self._pending_utterance_count - 1)

    async def _drain_pending_utterances(self) -> None:
        """Transcribe and process utterances that arrived while the bot was speaking.

        Waits for conversation to settle (1.5s of no new utterances) before
        draining. This prevents the bot from firing endlessly in active channels.
        Batches all queued utterances into one combined context for the LLM.
        """
        if not self._pending_utterances:
            return

        # Wait for conversation to settle — if people are still talking,
        # let them finish before responding
        settle_ms = 1500
        for _ in range(15):  # Max 15 * 100ms = 1.5s settle wait
            await asyncio.sleep(0.1)
            # Check if queue grew (someone still talking)
            queue_size = len(self._pending_utterances)
            if queue_size == 0:
                return  # Queue was cleared externally
            await asyncio.sleep(0.1)
            if len(self._pending_utterances) == queue_size:
                # Queue stable for 200ms — conversation has settled
                break

        if not self._pending_utterances:
            return

        # Grab and clear the queue
        queued = self._pending_utterances[:]
        self._pending_utterances.clear()

        log.info("Draining queued utterances", count=len(queued))

        # Transcribe all queued utterances in parallel
        transcription_tasks = [
            self._transcribe(pcm) for _uid, pcm in queued
        ]
        transcriptions = await asyncio.gather(*transcription_tasks, return_exceptions=True)

        # Build combined context
        combined_parts: list[str] = []
        last_user_id = None
        for (uid, _pcm), result in zip(queued, transcriptions):
            if isinstance(result, Exception) or not result or len(str(result).strip()) < 2:
                continue
            text = str(result).strip()
            combined_parts.append(text)
            last_user_id = uid

        if not combined_parts or last_user_id is None:
            return

        combined_text = " ".join(combined_parts)
        log.info(
            "Processing combined queued input",
            text=combined_text[:120],
            user=last_user_id,
            utterances=len(combined_parts),
        )

        # Process the combined input
        import time as _time
        t0 = _time.monotonic()

        full_response = ""
        first_sentence = True

        async for sentence in self.brain.respond_streaming(
            combined_text, str(last_user_id), guild_id=self._guild_id,
        ):
            full_response += sentence + " "

            if first_sentence:
                t_llm = _time.monotonic()
                log.info(
                    "First sentence ready",
                    llm_ms=int((t_llm - t0) * 1000),
                    sentence=sentence[:60],
                )
                first_sentence = False

            audio = await self._synthesize(sentence)
            if audio:
                while self.voice_client.is_playing():
                    await asyncio.sleep(0.05)
                await self._play_audio(audio)

        if full_response:
            log.info(
                "Response complete (queued)",
                user=last_user_id,
                response=full_response.strip()[:100],
            )

    async def _transcribe(self, pcm_audio: bytes) -> str:
        """Try STT providers in cascade order."""
        for provider in self.stt_providers:
            try:
                text = await provider.transcribe(pcm_audio)
                if text:
                    return text
            except Exception as exc:
                log.warning(
                    "STT provider failed, trying next",
                    provider=provider.name,
                    error=str(exc)[:80],
                )
        return ""

    async def _synthesize(self, text: str) -> bytes:
        """Try TTS providers in cascade order."""
        for provider in self.tts_providers:
            try:
                audio = await provider.synthesize(text)
                if audio:
                    log.info("TTS synthesized", provider=provider.name, bytes=len(audio))
                    return audio
            except Exception as exc:
                log.warning(
                    "TTS provider failed, trying next",
                    provider=provider.name,
                    error=str(exc)[:80],
                )
        return b""

    async def _synthesize_toob(self, text: str) -> bytes:
        """Synthesize text using Toob's voice — deep, menacing, warlord.

        Two-stage pipeline:
        1. Get raw audio from ANY TTS provider (Google Enceladus preferred,
           then full TTS cascade fallback — Edge TTS, Kokoro, etc.)
        2. Apply warlord FFmpeg filter chain (pitch down + bass + reverb)

        The FFmpeg processing works on any audio source, so Toob's voice
        identity is preserved even when Google TTS is down.
        """
        # --- Stage 1: Get raw audio (any provider) ---
        raw_audio = b""
        source = "unknown"

        # Try Google Enceladus first (Toob's preferred voice)
        try:
            from poob.voice.tts import GoogleCloudTTS
            for provider in self.tts_providers:
                if isinstance(provider, GoogleCloudTTS):
                    toob_tts = GoogleCloudTTS(
                        api_key=provider._api_key,
                        voice="en-US-Chirp3-HD-Enceladus",
                        speaking_rate=0.95,
                    )
                    raw_audio = await toob_tts.synthesize(text)
                    if raw_audio:
                        source = "google_enceladus"
                    break
        except Exception as exc:
            log.debug("Toob Google TTS failed", error=str(exc)[:60])

        # Fallback: use any available TTS provider from the cascade
        if not raw_audio:
            raw_audio = await self._synthesize(text)
            source = "cascade_fallback"

        if not raw_audio:
            return b""

        # --- Stage 2: Apply warlord FFmpeg filter (works on any audio) ---
        # Warlord filter chain — retuned April 22 2026 per user feedback
        # ("still too deep, could be faster"):
        # - asetrate=18500: pitch DOWN but less (24kHz→18.5kHz ≈ -4.5 semitones;
        #   was 16000 = -7 semitones which read as "Darth Vader deep"). Still
        #   menacing, but intelligible and clearly Toob-not-Poob.
        # - aresample=24000: resample back to playable rate.
        # - atempo=2.0: speed up more (was 1.85; user still wanted quicker).
        #   2.0 is atempo's per-stage max — any faster needs chained atempo.
        # - vibrato=f=5.5:d=0.15: subtle pitch wobble, breaks the monotone.
        #   f=5.5 Hz is natural speech-prosody territory (human vibrato is
        #   ~4-7 Hz); d=0.15 is shallow enough to stay menacing, not drunk.
        # - bass=g=6:f=80: lighter bass boost (was g=10 which amplified the
        #   deep-pitch effect — reducing with shallower asetrate).
        # - aecho=0.8:0.85:40:0.3: reverb (cavernous, menacing).
        # - volume=1.35: retained; speechnorm in _play_audio handles loudness.
        # The chain lives at module level so _prewarm_ffmpeg can exercise
        # the same path during startup without drift.
        filter_chain = _TOOB_FILTER_CHAIN

        # Pipe-based FFmpeg call — no tempfiles, no executor round-trip.
        # Input and output are MP3; `-analyzeduration 0 -probesize 32`
        # skip the format probe (we know it's MP3) and shave startup.
        try:
            proc = await asyncio.create_subprocess_exec(
                FFMPEG_PATH,
                "-hide_banner", "-loglevel", "error",
                "-analyzeduration", "0", "-probesize", "32",
                "-f", "mp3", "-i", "pipe:0",
                "-af", filter_chain,
                "-f", "mp3", "pipe:1",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(raw_audio), timeout=5.0,
                )
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                log.warning("Toob FFmpeg timeout")
                return raw_audio

            if proc.returncode == 0 and stdout:
                log.info(
                    "Toob TTS synthesized", bytes=len(stdout), source=source,
                )
                return stdout

            stderr_msg = (stderr or b"").decode("utf-8", errors="replace")[:80]
            log.warning("Toob FFmpeg failed", stderr=stderr_msg)
            return raw_audio
        except Exception as exc:
            log.warning("Toob FFmpeg processing error", error=str(exc)[:80])
            return raw_audio

    async def _synthesize_boob(self, text: str) -> bytes:
        """Synthesize text using Boob's voice — Toob's sweet side piece.

        Same two-stage shape as ``_synthesize_toob``: pull raw audio from
        a Google Chirp3-HD female voice (Leda), then apply the Boob
        FFmpeg filter chain (pitch up, faster, no reverb). Filter chain
        works on any audio, so the persona survives a TTS cascade
        fallback even when Google is down.

        See decisions/boob-music-wrap-variant for the design rationale.
        """
        # --- Stage 1: Get raw audio (Leda preferred, cascade fallback) ---
        raw_audio = b""
        source = "unknown"

        try:
            from poob.voice.tts import GoogleCloudTTS
            for provider in self.tts_providers:
                if isinstance(provider, GoogleCloudTTS):
                    boob_tts = GoogleCloudTTS(
                        api_key=provider._api_key,
                        voice="en-US-Chirp3-HD-Leda",
                        speaking_rate=1.05,
                    )
                    raw_audio = await boob_tts.synthesize(text)
                    if raw_audio:
                        source = "google_leda"
                    break
        except Exception as exc:
            log.debug("Boob Google TTS failed", error=str(exc)[:60])

        if not raw_audio:
            raw_audio = await self._synthesize(text)
            source = "cascade_fallback"

        if not raw_audio:
            return b""

        # --- Stage 2: Apply Boob filter chain (pitch up, brighter) ---
        filter_chain = _BOOB_FILTER_CHAIN

        try:
            proc = await asyncio.create_subprocess_exec(
                FFMPEG_PATH,
                "-hide_banner", "-loglevel", "error",
                "-analyzeduration", "0", "-probesize", "32",
                "-f", "mp3", "-i", "pipe:0",
                "-af", filter_chain,
                "-f", "mp3", "pipe:1",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(raw_audio), timeout=5.0,
                )
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                log.warning("Boob FFmpeg timeout")
                return raw_audio

            if proc.returncode == 0 and stdout:
                log.info(
                    "Boob TTS synthesized", bytes=len(stdout), source=source,
                )
                return stdout

            stderr_msg = (stderr or b"").decode("utf-8", errors="replace")[:80]
            log.warning("Boob FFmpeg failed", stderr=stderr_msg)
            return raw_audio
        except Exception as exc:
            log.warning("Boob FFmpeg processing error", error=str(exc)[:80])
            return raw_audio

    async def _play_audio(self, audio_data: bytes) -> None:
        """Play audio bytes through the Discord voice client.

        When music is playing (self.music_player is set and has an active mixer),
        TTS is injected as an overlay — music ducks automatically and Poob speaks
        over it. When no music is playing, TTS plays directly as before.

        Handles both MP3 (Edge TTS/Google TTS) and WAV (Kokoro) formats
        by routing through ffmpeg.

        Args:
            audio_data: Audio bytes (MP3 or WAV format).
        """
        if not audio_data or not self.voice_client.is_connected():
            return

        self._is_speaking = True
        log.info("Playing audio", bytes=len(audio_data))

        try:
            import tempfile
            import os

            # Write TTS audio to temp file — more reliable than pipe for ffmpeg
            tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
            tmp.write(audio_data)
            tmp.close()
            tmp_path = tmp.name

            # speechnorm normalizes TTS RMS toward peak with a built-in
            # limiter — fixes the root cause of "voice too quiet vs mastered
            # music" (TTS peaks at ~-16 LUFS, music at ~-9 LUFS). e=12.5 is
            # the expansion ceiling, r=0.0001 prevents pumping, l=1 keeps
            # peaks from clipping before the PCMVolumeTransformer stage.
            # Single-pass, no added latency — applies to both Poob and Toob
            # (Toob's filter_chain output also flows through here).
            tts_source = discord.FFmpegPCMAudio(
                tmp_path,
                executable=FFMPEG_PATH,
                options="-af speechnorm=e=12.5:r=0.0001:l=1",
            )

            # --- Music overlay path ---
            # If music is playing, inject TTS as overlay into the mixer.
            # Music ducks automatically (MixingAudioSource handles gain ramps).
            if (
                self.music_player is not None
                and self.music_player.mixer is not None
                and self.voice_client.is_playing()
            ):
                # After speechnorm, TTS already sits near peak. 3.0 provides
                # an extra +1.5 dB for presence over the ducked music bed
                # (25%); gentle clipping here gives speech the same "fullness"
                # as mastered music. Was 2.5 before speechnorm was added.
                boosted_tts = discord.PCMVolumeTransformer(tts_source, volume=3.0)
                self.music_player.inject_tts_overlay(boosted_tts)
                log.info("TTS injected as overlay on music")

                # Wait for overlay to finish (mixer auto-cleans up)
                await self.music_player.wait_overlay_done(timeout=30.0)

                # Clean up temp file
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                self._is_speaking = False
                return

            # --- Standard path (no music) ---
            # Post-speechnorm: TTS is already at broadcast-standard loudness.
            # 3.0 brings speech up to match mastered music when Poob is the
            # only thing playing on join. Was 2.5 before speechnorm was added.
            source = discord.PCMVolumeTransformer(tts_source, volume=3.0)

            # Create a future to await playback completion
            play_done = self._loop.create_future()

            def after_play(error: Exception | None) -> None:
                self._is_speaking = False
                if error:
                    log.warning("Playback error", error=str(error)[:80])
                # Clean up temp file
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                if not play_done.done():
                    self._loop.call_soon_threadsafe(play_done.set_result, None)

            vc = self.voice_client
            vc.play(source, after=after_play)
            log.info("Playback started", file=tmp_path)
            await play_done

        except Exception as exc:
            self._is_speaking = False
            log.error("Audio playback failed", error=str(exc)[:150])

    async def cleanup(self) -> None:
        """Clean up resources when leaving voice channel."""
        # Cancel any in-flight response tasks. Pre-`8b960bb` this
        # block referenced an undefined `_current_task` attribute and
        # crashed `/join` when the slash command tried to disconnect a
        # stale session before connecting fresh.
        for task in list(self._inflight_tasks):
            if not task.done():
                task.cancel()
        self._inflight_tasks.clear()

        # Flush all user buffers/detectors
        for buffer in self._user_buffers.values():
            buffer.flush()
        self._user_buffers.clear()
        for detector in self._speech_detectors.values():
            detector.flush()
        self._speech_detectors.clear()

        # Unlink music player (MusicCog handles its own cleanup)
        self.music_player = None

        # Clear conversation histories for all users in this session
        log.info("Voice session cleaned up")
