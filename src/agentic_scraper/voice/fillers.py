"""Pre-generated filler audio for masking processing latency.

Plays "thinking" sounds (hmm, let me think, etc.) immediately after
end-of-speech detection, buying 1-2 seconds while STT/LLM/TTS runs.
This is a pure UX trick — makes the bot feel instantly responsive.

Filler clips are generated once at startup using Edge TTS and cached
to disk. At runtime, a random clip is selected and played immediately.
"""

from __future__ import annotations

import asyncio
import os
import random
from pathlib import Path

from agentic_scraper.utils.logging import get_logger

log = get_logger("voice.fillers")

FILLER_PHRASES = [
    "Hmm...",
    "Let me think...",
    "Good question...",
    "One sec...",
    "Alright...",
    "So...",
    "Well...",
    "Okay...",
]

FILLER_DIR = Path("data/voice_fillers")


async def generate_fillers(
    voice: str = "en-US-GuyNeural",
    rate: str = "+0%",
) -> list[Path]:
    """Generate filler audio clips using Edge TTS.

    Creates MP3 files in data/voice_fillers/ if they don't exist.
    Called once at startup. Idempotent — skips existing files.

    Args:
        voice: Edge TTS voice name.
        rate: Speech rate adjustment.

    Returns:
        List of paths to filler audio files.
    """
    FILLER_DIR.mkdir(parents=True, exist_ok=True)

    paths: list[Path] = []
    for i, phrase in enumerate(FILLER_PHRASES):
        path = FILLER_DIR / f"filler_{i:02d}.mp3"
        paths.append(path)

        if path.exists() and path.stat().st_size > 0:
            continue

        try:
            import edge_tts

            communicate = edge_tts.Communicate(phrase, voice=voice, rate=rate)
            await communicate.save(str(path))
            log.debug("Generated filler", phrase=phrase, path=str(path))
        except Exception as exc:
            log.warning("Failed to generate filler", phrase=phrase, error=str(exc)[:80])

    existing = [p for p in paths if p.exists() and p.stat().st_size > 0]
    log.info("Filler audio ready", count=len(existing))
    return existing


class FillerPlayer:
    """Manages and plays random filler audio clips.

    Thread-safe: can be called from voice thread or async context.
    """

    def __init__(self, filler_paths: list[Path] | None = None) -> None:
        self._paths = filler_paths or []
        self._last_index = -1

    @property
    def available(self) -> bool:
        return len(self._paths) > 0

    def get_random_filler(self) -> Path | None:
        """Get a random filler path, avoiding immediate repeats."""
        if not self._paths:
            return None

        if len(self._paths) == 1:
            return self._paths[0]

        # Avoid repeating the same filler consecutively
        candidates = [i for i in range(len(self._paths)) if i != self._last_index]
        idx = random.choice(candidates)
        self._last_index = idx
        return self._paths[idx]

    def get_filler_bytes(self) -> bytes | None:
        """Read a random filler file into bytes."""
        path = self.get_random_filler()
        if path is None:
            return None
        try:
            return path.read_bytes()
        except OSError:
            return None
