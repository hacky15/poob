"""Speech-to-text providers for voice chat.

Primary: Groq Whisper API (~50ms for a 10s clip, free tier).
Fallback: faster-whisper running locally (needs GPU for best perf).
"""

from __future__ import annotations

import io
import struct
import wave
from typing import Protocol, runtime_checkable

from agentic_scraper.utils.logging import get_logger

log = get_logger("voice.stt")


@runtime_checkable
class STTProvider(Protocol):
    """Speech-to-text provider protocol."""

    @property
    def name(self) -> str: ...

    async def transcribe(self, pcm_audio: bytes, sample_rate: int = 48000) -> str:
        """Transcribe PCM audio bytes to text.

        Args:
            pcm_audio: Raw 16-bit signed LE PCM audio (mono).
            sample_rate: Sample rate of the audio (default 48kHz from Discord).

        Returns:
            Transcribed text, or empty string if nothing detected.
        """
        ...

    def is_available(self) -> bool:
        """Check if this provider is ready to use."""
        ...


def _pcm_to_wav(pcm_audio: bytes, sample_rate: int = 48000, channels: int = 1) -> bytes:
    """Convert raw PCM bytes to WAV format for API upload."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)  # 16-bit
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_audio)
    return buf.getvalue()


def _stereo_to_mono(pcm_stereo: bytes) -> bytes:
    """Convert 16-bit stereo PCM to mono by averaging channels."""
    samples = struct.unpack(f"<{len(pcm_stereo) // 2}h", pcm_stereo)
    mono = []
    for i in range(0, len(samples), 2):
        if i + 1 < len(samples):
            mono.append((samples[i] + samples[i + 1]) // 2)
        else:
            mono.append(samples[i])
    return struct.pack(f"<{len(mono)}h", *mono)


class GroqWhisperSTT:
    """Groq Whisper API — fastest cloud STT, free tier.

    Uses whisper-large-v3-turbo for ~50ms transcription of short clips.
    """

    def __init__(self, api_key: str, model: str = "whisper-large-v3-turbo") -> None:
        self._api_key = api_key
        self._model = model
        self._client: object | None = None

    @property
    def name(self) -> str:
        return f"groq_whisper:{self._model}"

    def _get_client(self) -> object:
        if self._client is None:
            from groq import AsyncGroq

            self._client = AsyncGroq(api_key=self._api_key)
        return self._client

    async def transcribe(self, pcm_audio: bytes, sample_rate: int = 48000) -> str:
        """Transcribe PCM audio via Groq Whisper API."""
        if len(pcm_audio) < 3200:
            return ""

        # Convert stereo to mono if needed (Discord sends stereo)
        # Heuristic: if sample count is even and data is large, assume stereo
        mono_audio = _stereo_to_mono(pcm_audio)
        wav_data = _pcm_to_wav(mono_audio, sample_rate=sample_rate, channels=1)

        try:
            client = self._get_client()
            transcription = await client.audio.transcriptions.create(  # type: ignore[union-attr]
                file=("utterance.wav", wav_data),
                model=self._model,
                language="en",
                response_format="text",
            )
            text = str(transcription).strip()
            if text:
                log.debug("Groq Whisper transcribed", length=len(text), text=text[:80])
            return text
        except Exception as exc:
            log.warning("Groq Whisper STT failed", error=str(exc)[:120])
            return ""

    def is_available(self) -> bool:
        return bool(self._api_key)


class GeminiSTT:
    """Gemini Flash — native audio understanding for STT.

    Uses Gemini's multimodal capabilities to transcribe audio.
    Far more accurate than Whisper for names, slang, and unusual words
    because Gemini "understands" audio natively rather than through a
    speech-specific encoder.

    Free tier: 15 RPM, 1M tokens/day. Paid: $0.10/M text + $0.70/M audio tokens.
    """

    def __init__(self, api_key: str, model: str = "gemini-2.5-flash-lite") -> None:
        self._api_key = api_key
        self._model = model
        self._client = None

    @property
    def name(self) -> str:
        return f"gemini_stt:{self._model}"

    def _get_client(self):
        if self._client is None:
            from google import genai
            self._client = genai.Client(api_key=self._api_key)
        return self._client

    async def transcribe(self, pcm_audio: bytes, sample_rate: int = 48000) -> str:
        """Transcribe PCM audio via Gemini's native audio understanding."""
        if len(pcm_audio) < 3200:
            return ""

        mono_audio = _stereo_to_mono(pcm_audio)
        wav_data = _pcm_to_wav(mono_audio, sample_rate=sample_rate, channels=1)

        try:
            from google import genai as _genai

            client = self._get_client()
            response = await client.aio.models.generate_content(
                model=self._model,
                contents=[
                    _genai.types.Content(
                        parts=[
                            _genai.types.Part(
                                inline_data=_genai.types.Blob(
                                    mime_type="audio/wav",
                                    data=wav_data,
                                )
                            ),
                            _genai.types.Part(
                                text=(
                                    "Transcribe exactly what is said in this audio. "
                                    "Return ONLY the transcription text, nothing else. "
                                    "If there is no speech, return an empty string."
                                )
                            ),
                        ]
                    )
                ],
            )
            text = response.text.strip() if response.text else ""
            # Clean up common Gemini artifacts
            if text.lower() in ("(silence)", "(no speech)", ""):
                return ""
            if text:
                log.debug("Gemini STT transcribed", length=len(text), text=text[:80])
            return text
        except Exception as exc:
            log.warning("Gemini STT failed", error=str(exc)[:150])
            return ""

    def is_available(self) -> bool:
        return bool(self._api_key)


class DeepgramSTT:
    """Deepgram Nova-3 — most accurate production STT.

    $200 free credit (~433 hours). Nova-3 is significantly more accurate
    than Whisper for names, slang, and conversational speech.
    Uses the pre-recorded (batch) API for simplicity.
    """

    def __init__(self, api_key: str, model: str = "nova-3") -> None:
        self._api_key = api_key
        self._model = model

    @property
    def name(self) -> str:
        return f"deepgram:{self._model}"

    async def transcribe(self, pcm_audio: bytes, sample_rate: int = 48000) -> str:
        """Transcribe PCM audio via Deepgram pre-recorded API."""
        if len(pcm_audio) < 3200:
            return ""

        mono_audio = _stereo_to_mono(pcm_audio)
        wav_data = _pcm_to_wav(mono_audio, sample_rate=sample_rate, channels=1)

        try:
            import httpx

            async with httpx.AsyncClient(timeout=8.0) as client:
                r = await client.post(
                    "https://api.deepgram.com/v1/listen",
                    headers={
                        "Authorization": f"Token {self._api_key}",
                        "Content-Type": "audio/wav",
                    },
                    params={
                        "model": self._model,
                        "language": "en",
                        "punctuate": "true",
                        "smart_format": "true",
                    },
                    content=wav_data,
                )
                r.raise_for_status()
                data = r.json()

            # Extract transcript from response
            channels = data.get("results", {}).get("channels", [])
            if not channels:
                return ""
            alternatives = channels[0].get("alternatives", [])
            if not alternatives:
                return ""
            text = alternatives[0].get("transcript", "").strip()

            if text:
                log.debug(
                    "Deepgram transcribed",
                    length=len(text),
                    text=text[:80],
                    confidence=alternatives[0].get("confidence", 0),
                )
            return text
        except httpx.HTTPStatusError as exc:
            log.warning(
                "Deepgram STT failed",
                status=exc.response.status_code,
                body=exc.response.text[:300],
            )
            return ""
        except Exception as exc:
            log.warning("Deepgram STT failed", error=str(exc)[:200])
            return ""

    def is_available(self) -> bool:
        return bool(self._api_key)


class LocalWhisperSTT:
    """Local faster-whisper STT fallback.

    Requires `faster-whisper` pip package and a downloaded model.
    Best with CUDA GPU; works on CPU but slower.
    """

    def __init__(self, model_size: str = "base.en") -> None:
        self._model_size = model_size
        self._model: object | None = None
        self._available: bool | None = None

    @property
    def name(self) -> str:
        return f"local_whisper:{self._model_size}"

    def _get_model(self) -> object:
        if self._model is None:
            from faster_whisper import WhisperModel

            self._model = WhisperModel(
                self._model_size,
                device="auto",
                compute_type="auto",
            )
        return self._model

    async def transcribe(self, pcm_audio: bytes, sample_rate: int = 48000) -> str:
        """Transcribe PCM audio via local faster-whisper."""
        import asyncio

        if len(pcm_audio) < 3200:
            return ""

        mono_audio = _stereo_to_mono(pcm_audio)
        wav_data = _pcm_to_wav(mono_audio, sample_rate=sample_rate, channels=1)

        def _sync_transcribe() -> str:
            import numpy as np

            model = self._get_model()
            # Decode WAV to float32 numpy array
            buf = io.BytesIO(wav_data)
            with wave.open(buf, "rb") as wf:
                frames = wf.readframes(wf.getnframes())
            samples = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
            segments, _ = model.transcribe(samples, language="en")  # type: ignore[union-attr]
            return " ".join(seg.text.strip() for seg in segments).strip()

        try:
            loop = asyncio.get_running_loop()
            text = await loop.run_in_executor(None, _sync_transcribe)
            if text:
                log.debug("Local Whisper transcribed", length=len(text), text=text[:80])
            return text
        except Exception as exc:
            log.warning("Local Whisper STT failed", error=str(exc)[:120])
            return ""

    def is_available(self) -> bool:
        if self._available is None:
            try:
                import faster_whisper  # noqa: F401

                self._available = True
            except ImportError:
                self._available = False
        return self._available


def build_stt_cascade(
    groq_api_key: str = "",
    deepgram_api_key: str = "",
    google_api_key: str = "",
    preferred: str = "gemini",
) -> list:
    """Build ordered list of STT providers (best first).

    Default order: Gemini Flash → Deepgram Nova-3 → Groq Whisper → Local Whisper.
    Gemini understands audio natively — far more accurate for names and unusual
    words like "Poob" that trip up traditional STT models.

    Args:
        groq_api_key: Groq API key for Whisper.
        deepgram_api_key: Deepgram API key for Nova-3.
        google_api_key: Google API key for Gemini Flash.
        preferred: Which provider to try first.

    Returns:
        List of STT providers to try in order.
    """
    providers: list = []

    gemini_stt = GeminiSTT(api_key=google_api_key)
    deepgram_stt = DeepgramSTT(api_key=deepgram_api_key)
    groq_stt = GroqWhisperSTT(api_key=groq_api_key)
    local_stt = LocalWhisperSTT()

    # Build cascade based on preference
    all_providers = {
        "gemini": gemini_stt,
        "deepgram": deepgram_stt,
        "groq_whisper": groq_stt,
        "local_whisper": local_stt,
    }

    # Preferred first, then rest in quality order
    order = [preferred] + [k for k in ["gemini", "deepgram", "groq_whisper", "local_whisper"] if k != preferred]

    for name in order:
        p = all_providers.get(name)
        if p and p.is_available():
            providers.append(p)

    names = [p.name for p in providers]
    log.info("STT cascade built", providers=names)
    return providers
