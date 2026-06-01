"""Pre-generated filler audio for masking processing latency.

Plays a quick in-character "thinking" NOISE the instant Poob is addressed,
masking the LLM+TTS delay before the real response. A pure UX trick — makes
the bot feel instantly responsive.

Clips are synthesized ONCE at startup and cached to disk; at runtime a random
clip is just read off disk and played (zero TTS in the hot path, so the
generating voice has no effect on latency). They're generated in Poob's own
voice (Google Fenrir) when a synthesizer is supplied, so the noise sounds like
Poob — not a stranger. Edge TTS is the no-key fallback. Cached by a
(voice, phrase) hash, so changing the voice or the phrases regenerates and the
stale clips are pruned. See docs/decisions/poob-noise-fillers.md.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import random
from pathlib import Path
from typing import TYPE_CHECKING

from poob.utils.logging import get_logger

if TYPE_CHECKING:
    from poob.voice.tts import TTSProvider

log = get_logger("voice.fillers")

# Quick, purposeful Poob thinking-noises — not words. Spelled to coax a
# vocalization out of the TTS rather than a spelled-out reading. Tunable;
# the operator can swap these freely (cache invalidates on change).
FILLER_PHRASES = [
    "Hmmmm.",
    "Mmmh, hmm.",
    "Aughhh.",
    "Aughh, yeah, aughh.",
    "Hmmph.",
    "Mmmnghh.",
    "Ohh, hmmm.",
    "Ughhh, hmm.",
]

FILLER_DIR = Path("data/voice_fillers")


async def _synth_one(synthesizer: TTSProvider | None, phrase: str) -> bytes:
    """Synthesize one clip, returning MP3 bytes (b"" on failure)."""
    if synthesizer is None:
        return b""
    try:
        return await synthesizer.synthesize(phrase) or b""
    except Exception as exc:
        log.debug("Filler synth failed", phrase=phrase, error=str(exc)[:80])
        return b""


async def generate_fillers(
    synthesizer: TTSProvider | None = None,
    *,
    phrases: list[str] | None = None,
    edge_fallback_voice: str = "en-US-RogerNeural",
) -> list[Path]:
    """Pre-generate the cached filler clips.

    Synthesizes each phrase with ``synthesizer`` (pass Poob's Google voice so
    the noise sounds like Poob); falls back to Edge TTS per-clip if the
    synthesizer is absent or fails. Idempotent and cache-keyed by
    (voice, phrase) hash — changing the voice or the phrase list produces new
    filenames and prunes the stale clips (so old word-fillers don't linger on
    the volume). Called once at startup.

    Returns the list of ready clip paths.
    """
    phrases = phrases or FILLER_PHRASES
    FILLER_DIR.mkdir(parents=True, exist_ok=True)

    voice_label = getattr(synthesizer, "name", None) or f"edge:{edge_fallback_voice}"
    # Map the desired clip files for this (voice, phrase-set).
    wanted: dict[Path, str] = {}
    for phrase in phrases:
        digest = hashlib.sha1(f"{voice_label}|{phrase}".encode()).hexdigest()[:12]
        wanted[FILLER_DIR / f"filler_{digest}.mp3"] = phrase

    # Prune stale clips (old voice/phrases) so the dir only holds the current set.
    for old in FILLER_DIR.glob("filler_*.mp3"):
        if old not in wanted:
            try:
                old.unlink()
            except OSError:
                pass

    edge: TTSProvider | None = None
    for path, phrase in wanted.items():
        if path.exists() and path.stat().st_size > 0:
            continue
        audio = await _synth_one(synthesizer, phrase)
        if not audio:
            if edge is None:
                try:
                    from poob.voice.tts import EdgeTTS

                    edge = EdgeTTS(voice=edge_fallback_voice, rate="+0%")
                except Exception:
                    edge = None
            audio = await _synth_one(edge, phrase)
        if audio:
            try:
                path.write_bytes(audio)
                log.debug("Generated filler", phrase=phrase, path=str(path))
            except OSError as exc:
                log.warning("Failed to write filler", phrase=phrase, error=str(exc)[:80])

    existing = [p for p in wanted if p.exists() and p.stat().st_size > 0]
    log.info("Filler audio ready", count=len(existing), voice=voice_label)
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
