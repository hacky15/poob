"""Edge TTS engine — multi-voice + multi-rate generator.

47 English voices × 8 speaking rates = 376 acoustic identities per phrase
before any post-process augmentation. Edge TTS is free, unlimited, and
covers 11 English regions (US, GB, AU, CA, IN, IE, HK, KE, NZ, NG, PH,
SG, ZA, TZ) which gives accent diversity for free.

Two failure modes to handle robustly:

  1. Network hiccups — Edge TTS occasionally returns < 500 bytes of MP3
     (effectively silence). Retry once with backoff; on second failure,
     skip the sample cleanly.

  2. Rate-limit / API change — rare, but worth wrapping. Caller decides
     whether to abort or continue.

The orchestrator handles concurrency (asyncio.gather batches) so this
module stays single-sample-focused.
"""

from __future__ import annotations

import asyncio
import io

from .audio_utils import mp3_to_wav_16k_mono


# ---------------------------------------------------------------------------
# Full English voice roster — all 47 Edge TTS English voices as of 2026
# ---------------------------------------------------------------------------

ALL_VOICES: tuple[str, ...] = (
    # US (17)
    "en-US-AnaNeural", "en-US-AndrewNeural", "en-US-AndrewMultilingualNeural",
    "en-US-AriaNeural", "en-US-AvaNeural", "en-US-AvaMultilingualNeural",
    "en-US-BrianNeural", "en-US-BrianMultilingualNeural",
    "en-US-ChristopherNeural", "en-US-EmmaNeural", "en-US-EmmaMultilingualNeural",
    "en-US-EricNeural", "en-US-GuyNeural", "en-US-JennyNeural",
    "en-US-MichelleNeural", "en-US-RogerNeural", "en-US-SteffanNeural",
    # GB (5)
    "en-GB-LibbyNeural", "en-GB-MaisieNeural", "en-GB-RyanNeural",
    "en-GB-SoniaNeural", "en-GB-ThomasNeural",
    # AU (2)
    "en-AU-NatashaNeural", "en-AU-WilliamMultilingualNeural",
    # CA (2)
    "en-CA-ClaraNeural", "en-CA-LiamNeural",
    # IN (3)
    "en-IN-NeerjaNeural", "en-IN-NeerjaExpressiveNeural", "en-IN-PrabhatNeural",
    # IE (2)
    "en-IE-ConnorNeural", "en-IE-EmilyNeural",
    # Other regional (14)
    "en-HK-YanNeural", "en-HK-SamNeural",
    "en-KE-AsiliaNeural", "en-KE-ChilembaNeural",
    "en-NZ-MitchellNeural", "en-NZ-MollyNeural",
    "en-NG-AbeoNeural", "en-NG-EzinneNeural",
    "en-PH-JamesNeural", "en-PH-RosaNeural",
    "en-SG-LunaNeural", "en-SG-WayneNeural",
    "en-ZA-LeahNeural", "en-ZA-LukeNeural",
    "en-TZ-ElimuNeural", "en-TZ-ImaniNeural",
)

# ---------------------------------------------------------------------------
# Speaking-rate ladder — covers the realistic range of human delivery
# (slow, half-asleep at -30% through fast, urgent at +30%).
# ---------------------------------------------------------------------------

ALL_RATES: tuple[str, ...] = (
    "-30%", "-20%", "-10%", "-5%", "+0%", "+10%", "+20%", "+30%",
)


async def synth_one(
    voice: str,
    text: str,
    rate: str,
    *,
    retries: int = 2,
) -> bytes | None:
    """Generate one TTS sample → 16 kHz mono WAV bytes (or ``None``).

    Backs off 1 s, then 3 s on network failures. Anything under 500 bytes
    of MP3 is treated as silence and discarded.
    """
    import edge_tts
    for attempt in range(retries + 1):
        try:
            communicate = edge_tts.Communicate(text, voice=voice, rate=rate)
            buf = io.BytesIO()
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    buf.write(chunk["data"])
            mp3_bytes = buf.getvalue()
            if len(mp3_bytes) < 500:
                return None
            return mp3_to_wav_16k_mono(mp3_bytes)
        except Exception:
            if attempt < retries:
                await asyncio.sleep(1 + attempt * 2)
            continue
    return None


def sanitize_filename_component(text: str, max_len: int = 24) -> str:
    """Build a filesystem-safe tag from arbitrary phrase / param text.

    Lowercase, alphanumerics + ``_`` only. Truncated to ``max_len``.
    """
    out = []
    for ch in text:
        if ch.isalnum():
            out.append(ch.lower())
        else:
            out.append("_")
    s = "".join(out).strip("_")
    while "__" in s:
        s = s.replace("__", "_")
    return s[:max_len]


def voice_tag(voice: str) -> str:
    """Short, filename-safe identifier for a voice."""
    return (
        voice.replace("Neural", "").replace("Multilingual", "M").replace("-", "_")
    ).lower()


def rate_tag(rate: str) -> str:
    return rate.replace("+", "p").replace("-", "m").replace("%", "")
