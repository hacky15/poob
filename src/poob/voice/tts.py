"""Text-to-speech providers for voice chat.

Cascade: Google Cloud TTS (Neural2/Studio) → Edge TTS → Kokoro (local).
Google Cloud TTS provides flagship-quality neural voices with natural
prosody, emphasis, and correct pronunciation. Free tier: 1M chars/month.
"""

from __future__ import annotations

import asyncio
import io
from typing import Protocol, runtime_checkable

from poob.utils.logging import get_logger

log = get_logger("voice.tts")


@runtime_checkable
class TTSProvider(Protocol):
    """Text-to-speech provider protocol."""

    @property
    def name(self) -> str: ...

    async def synthesize(self, text: str) -> bytes:
        """Convert text to audio bytes (MP3 or WAV format).

        Args:
            text: Text to speak.

        Returns:
            Audio bytes ready for ffmpeg decoding.
        """
        ...

    def is_available(self) -> bool:
        """Check if this provider is ready to use."""
        ...


class GoogleCloudTTS:
    """Google Cloud Text-to-Speech — Neural2/Studio voices via REST API.

    Uses the same API key as Google Cloud Vision (no service account needed).
    REST endpoint: texttospeech.googleapis.com/v1/text:synthesize

    Free tier: 1M characters/month (Neural2), 100K chars/month (Studio).
    """

    def __init__(
        self,
        api_key: str = "",
        voice: str = "en-US-Neural2-D",
        speaking_rate: float = 1.0,
    ) -> None:
        self._api_key = api_key
        self._voice_name = voice
        self._speaking_rate = speaking_rate
        self._available: bool | None = None

    @property
    def name(self) -> str:
        return f"google_tts:{self._voice_name}"

    async def synthesize(self, text: str) -> bytes:
        """Convert text to MP3 via Google Cloud TTS REST API."""
        import base64
        import httpx

        url = f"https://texttospeech.googleapis.com/v1/text:synthesize?key={self._api_key}"

        payload = {
            "input": {"text": text},
            "voice": {
                "languageCode": self._voice_name[:5],
                "name": self._voice_name,
            },
            "audioConfig": {
                "audioEncoding": "MP3",
                "speakingRate": self._speaking_rate,
            },
        }

        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(url, json=payload)
            response.raise_for_status()
            data = response.json()

        mp3_data = base64.b64decode(data["audioContent"])
        log.debug("Google Cloud TTS synthesized", voice=self._voice_name, bytes=len(mp3_data))
        return mp3_data

    def is_available(self) -> bool:
        if self._available is not None:
            return self._available
        self._available = bool(self._api_key)
        if not self._available:
            log.info("Google Cloud TTS not available (no API key)")
        return self._available


class EdgeTTS:
    """Microsoft Edge TTS — free, no API key, decent quality voices.

    Uses the edge-tts library which leverages Microsoft Edge's
    Read Aloud backend. Risk: unofficial API, could be rate-limited.
    """

    def __init__(
        self,
        voice: str = "en-US-GuyNeural",
        rate: str = "+10%",
        pitch: str = "+0Hz",
    ) -> None:
        self._voice = voice
        self._rate = rate
        self._pitch = pitch
        self._available: bool | None = None

    @property
    def name(self) -> str:
        return f"edge_tts:{self._voice}"

    async def synthesize(self, text: str) -> bytes:
        """Convert text to MP3 via Edge TTS."""
        import edge_tts

        communicate = edge_tts.Communicate(
            text,
            voice=self._voice,
            rate=self._rate,
            pitch=self._pitch,
        )

        audio_chunks: list[bytes] = []
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                audio_chunks.append(chunk["data"])

        mp3_data = b"".join(audio_chunks)
        log.debug("Edge TTS synthesized", voice=self._voice, bytes=len(mp3_data))
        return mp3_data

    async def synthesize_streaming(self, text: str) -> list[bytes]:
        """Synthesize and return chunks as they arrive (for lower TTFB)."""
        import edge_tts

        communicate = edge_tts.Communicate(
            text,
            voice=self._voice,
            rate=self._rate,
            pitch=self._pitch,
        )

        chunks: list[bytes] = []
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                chunks.append(chunk["data"])
        return chunks

    def is_available(self) -> bool:
        if self._available is None:
            try:
                import edge_tts  # noqa: F401

                self._available = True
            except ImportError:
                self._available = False
        return self._available


class KokoroTTS:
    """Kokoro TTS — lightweight local model (82M params).

    Requires `kokoro-onnx` package. 96x real-time on GPU, 3-5x on CPU.
    """

    def __init__(self, voice: str = "af_heart", speed: float = 1.1) -> None:
        self._voice = voice
        self._speed = speed
        self._model: object | None = None
        self._available: bool | None = None

    @property
    def name(self) -> str:
        return f"kokoro:{self._voice}"

    def _get_model(self) -> object:
        if self._model is None:
            from kokoro_onnx import Kokoro

            self._model = Kokoro()
        return self._model

    async def synthesize(self, text: str) -> bytes:
        """Convert text to WAV via Kokoro."""
        loop = asyncio.get_running_loop()

        def _sync_generate() -> bytes:
            import numpy as np

            model = self._get_model()
            samples, sr = model.create(text, voice=self._voice, speed=self._speed)  # type: ignore[union-attr]

            pcm = (samples * 32767).astype(np.int16)
            buf = io.BytesIO()
            import wave

            with wave.open(buf, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(sr)
                wf.writeframes(pcm.tobytes())
            return buf.getvalue()

        try:
            wav_data = await loop.run_in_executor(None, _sync_generate)
            log.debug("Kokoro TTS synthesized", voice=self._voice, bytes=len(wav_data))
            return wav_data
        except Exception as exc:
            log.warning("Kokoro TTS failed", error=str(exc)[:120])
            return b""

    def is_available(self) -> bool:
        if self._available is None:
            try:
                import kokoro_onnx  # noqa: F401

                self._available = True
            except ImportError:
                self._available = False
        return self._available


def build_tts_cascade(
    preferred: str = "google_tts",
    voice: str = "en-US-Neural2-D",
    rate: str = "+10%",
    google_api_key: str = "",
    google_voice: str = "en-US-Neural2-D",
    google_speaking_rate: float = 1.05,
) -> list[GoogleCloudTTS | EdgeTTS | KokoroTTS]:
    """Build ordered list of TTS providers.

    Default cascade: Google Cloud TTS → Edge TTS → Kokoro.
    Google Cloud provides flagship-quality neural voices.
    Edge TTS is free fallback. Kokoro is local fallback.

    Args:
        preferred: Which provider to try first (google_tts, edge_tts, kokoro).
        voice: Voice name for Edge TTS.
        rate: Speech rate for Edge TTS.
        google_api_key: Google Cloud API key (same as Vision API).
        google_voice: Voice name for Google Cloud TTS.
        google_speaking_rate: Speaking rate multiplier for Google (1.0 = normal).

    Returns:
        List of TTS providers to try in order.
    """
    google = GoogleCloudTTS(
        api_key=google_api_key, voice=google_voice, speaking_rate=google_speaking_rate
    )
    edge = EdgeTTS(voice=voice, rate=rate)
    kokoro = KokoroTTS()

    all_providers = {
        "google_tts": google,
        "edge_tts": edge,
        "kokoro": kokoro,
    }

    providers: list[GoogleCloudTTS | EdgeTTS | KokoroTTS] = []

    # Add preferred first
    if preferred in all_providers:
        p = all_providers.pop(preferred)
        if p.is_available():
            providers.append(p)

    # Add remaining as fallbacks
    for p in all_providers.values():
        if p.is_available():
            providers.append(p)

    names = [p.name for p in providers]
    log.info("TTS cascade built", providers=names)
    return providers
