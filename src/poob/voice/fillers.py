"""Pre-generated filler audio for masking processing latency.

Plays a quick in-character "thinking" NOISE the instant Poob is addressed,
masking the LLM+TTS delay before the real response. A pure UX trick — makes
the bot feel instantly responsive.

Clips are synthesized ONCE at startup and cached to disk; at runtime a random
clip is just read off disk and played (zero TTS in the hot path, so the
generating voice has no effect on latency). They're generated in Poob's own
voice (Google Fenrir) when a synth factory is supplied, so the noise sounds
like Poob — not a stranger. Edge TTS is the no-key fallback.

Each phrase carries its own ``(speaking_rate, weight)``:
  - rate — the moans were workshopped at different speeds; slower (0.7-0.8)
    reads as a drawn-out moan, so the rate is per-phrase, not global.
  - weight — relative selection probability (the hero "Aughhhh." comes up
    ~30%, the occasional cringe intros ~once in a while).
Clips are cache-keyed by a ``(voice, rate, phrase)`` hash, so the same phrase
at two rates is two distinct clips and any change regenerates + prunes stale
clips. See docs/decisions/poob-noise-fillers.md.
"""

from __future__ import annotations

import hashlib
import random
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from poob.utils.logging import get_logger

if TYPE_CHECKING:
    from poob.voice.tts import TTSProvider

log = get_logger("voice.fillers")

# (phrase, speaking_rate, relative_weight). RE-WORKSHOPPED 2026-06-10 with the
# operator (docs/decisions/poob-noise-fillers.md "Update 2026-06-10"): a fresh
# listen-and-pick over candidates from tests/manual/gen_filler_candidates.py.
# Every clip now sits in the slow 0.76-0.82 "drawn-out moan" zone. The old roster
# forced 6 clips to rate 1.25 (fast, un-moany) ONLY because Fenrir spelled
# repeated consonants out at slow rates; the re-workshop confirmed vowel-dominant
# spellings (incl. "Ughhh"/"Uuughhh") vocalize fine at 0.78-0.80 — so the 1.25
# hack is GONE, not worked around. Loudness is fixed separately (gentle loudnorm
# at playback, not speech speechnorm — see pick_filler_treatment). "Oh man,
# aughh." is the one approved quip+moan; the rest are bare moans, de-duped.
FILLER_PHRASES: list[tuple[str, float, float]] = [
    ("Uuughhh.", 0.78, 18.0),       # hero — operator "very very good"
    ("Aughhh.", 0.80, 12.0),
    ("Ohhh.", 0.80, 12.0),
    ("Aaughhh.", 0.78, 10.0),
    ("Ohhhhh.", 0.76, 10.0),
    ("Oohhh.", 0.80, 10.0),
    ("Aohh.", 0.80, 10.0),
    ("Ughhh.", 0.80, 10.0),         # vocalizes at 0.80 — the spell-out hack is gone
    ("Uhmm.", 0.82, 8.0),
    ("Oh man, aughh.", 0.82, 6.0),  # quip+moan middle-ground — rarer
]


# Filler loudness, applied at playback (session._play_audio's ``af``/``volume``).
# Re-workshopped 2026-06-10: the soft moans must NOT go through the speech
# ``speechnorm`` (e=12.5 over-expands them harsh — the bug). The normal sound is
# a gentle ``loudnorm``; the loud speech-style treatment surfaces rarely for
# chaotic charm (operator: "B normal, C rare"). Same ffmpeg pass that already
# ran at playback, so no added hot-path latency.
FILLER_AF_GENTLE = "loudnorm=I=-16:TP=-1.5:LRA=11"
FILLER_VOL_GENTLE = 1.0
FILLER_AF_LOUD = "speechnorm=e=12.5:r=0.0001:l=1"
FILLER_VOL_LOUD = 3.0
FILLER_LOUD_PROB = 0.12  # ~1 in 8 fillers plays the loud variant


def pick_filler_treatment() -> tuple[str, float]:
    """Return ``(-af chain, volume)`` for one filler play — gentle by default,
    the loud variant ~12% of the time for charm."""
    if random.random() < FILLER_LOUD_PROB:
        return FILLER_AF_LOUD, FILLER_VOL_LOUD
    return FILLER_AF_GENTLE, FILLER_VOL_GENTLE

# Join "catchphrases" — cringe 2-3 word quip + an approved moan tail, played
# once when Poob ENTERS a voice channel (session.play_entrance), NOT as a
# mid-conversation latency mask. Weighted: "Daddy's home" ~25%, "Poob has
# arrived" ~10%, the rest split the remainder. Synthesized live at JOIN_RATE
# (the entrance isn't latency-critical). Tails are workshopped to render as a
# moan, not spelled letters, at this rate. See docs/decisions/poob-noise-fillers.md.
JOIN_RATE = 0.85
JOIN_PHRASES: list[tuple[str, float]] = [
    ("Daddy's home, ohhh yeah.", 25.0),
    ("Poob has arrived, aaaughhh.", 10.0),
    ("It's Poob here, aughh yeah.", 8.1),
    ("Poob's back, ohhh.", 8.1),
    ("Yeah it's Poob, uuughhh.", 8.1),
    ("Guess who? Poob! Aughh.", 8.1),
    ("Your boy Poob, auugh.", 8.1),
    ("Poob in the house, auugh.", 8.1),
    ("It's ya boy Poob, ohhh.", 8.1),
    ("Poob reporting, auuughhh.", 8.1),
]


def pick_join_phrase() -> str:
    """Weighted-random Poob entrance catchphrase from ``JOIN_PHRASES``."""
    phrases = [p for p, _w in JOIN_PHRASES]
    weights = [w for _p, w in JOIN_PHRASES]
    return random.choices(phrases, weights=weights, k=1)[0]


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
    synth_factory: Callable[[float], TTSProvider] | None = None,
    *,
    phrases: list[tuple[str, float, float]] | None = None,
    edge_fallback_voice: str = "en-US-RogerNeural",
) -> list[tuple[Path, float]]:
    """Pre-generate the cached filler clips.

    Args:
        synth_factory: builds a ``TTSProvider`` for a given speaking rate (pass
            a Google-Fenrir factory so the noise sounds like Poob). ``None``
            uses the Edge fallback for every clip.
        phrases: ``(phrase, rate, weight)`` triples; defaults to ``FILLER_PHRASES``.
        edge_fallback_voice: Edge voice used per-clip when the factory is absent
            or fails (degraded fallback; rate fidelity not preserved here).

    Synthesizes each phrase at ITS rate. Idempotent and cache-keyed by a
    ``(voice, rate, phrase)`` hash — the same phrase at two rates is two clips,
    and changing the voice/rate/phrase set regenerates + prunes stale clips.
    Called once at startup.

    Returns ``(clip_path, weight)`` pairs for the ready clips.
    """
    phrases = phrases or FILLER_PHRASES
    FILLER_DIR.mkdir(parents=True, exist_ok=True)

    probe = synth_factory(1.0) if synth_factory else None
    voice_label = getattr(probe, "name", None) or f"edge:{edge_fallback_voice}"

    # path -> (phrase, rate, weight). Rate is in the cache key so the same
    # phrase at two rates produces two distinct files.
    wanted: dict[Path, tuple[str, float, float]] = {}
    for phrase, rate, weight in phrases:
        digest = hashlib.sha1(f"{voice_label}|{rate}|{phrase}".encode()).hexdigest()[:12]
        wanted[FILLER_DIR / f"filler_{digest}.mp3"] = (phrase, rate, weight)

    # Prune stale clips (old voice/rate/phrases).
    for old in FILLER_DIR.glob("filler_*.mp3"):
        if old not in wanted:
            try:
                old.unlink()
            except OSError:
                pass

    edge: TTSProvider | None = None
    edge_tried = False
    results: list[tuple[Path, float]] = []
    for path, (phrase, rate, weight) in wanted.items():
        if not (path.exists() and path.stat().st_size > 0):
            audio = await _synth_one(synth_factory(rate) if synth_factory else None, phrase)
            if not audio:
                if not edge_tried:
                    edge_tried = True
                    try:
                        from poob.voice.tts import EdgeTTS

                        edge = EdgeTTS(voice=edge_fallback_voice, rate="+0%")
                    except Exception:
                        edge = None
                audio = await _synth_one(edge, phrase)
            if audio:
                try:
                    path.write_bytes(audio)
                    log.debug("Generated filler", phrase=phrase, rate=rate, path=str(path))
                except OSError as exc:
                    log.warning("Failed to write filler", phrase=phrase, error=str(exc)[:80])
        if path.exists() and path.stat().st_size > 0:
            results.append((path, weight))

    log.info("Filler audio ready", count=len(results), voice=voice_label)
    return results


class FillerPlayer:
    """Manages and plays random filler audio clips with weighted selection.

    Accepts either bare ``Path`` entries (equal weight) or ``(Path, weight)``
    pairs (as returned by :func:`generate_fillers`). Thread-safe enough for the
    voice thread: a single weighted pick with no shared mutable beyond
    ``_last_index``.
    """

    def __init__(
        self, filler_paths: list[Path] | list[tuple[Path, float]] | None = None
    ) -> None:
        self._entries: list[tuple[Path, float]] = []
        for item in filler_paths or []:
            if isinstance(item, tuple):
                path, weight = item
                self._entries.append((path, float(weight)))
            else:
                self._entries.append((item, 1.0))
        self._last_index = -1

    @property
    def available(self) -> bool:
        return len(self._entries) > 0

    def get_random_filler(self) -> Path | None:
        """Weighted-random clip path, avoiding an immediate repeat."""
        if not self._entries:
            return None
        if len(self._entries) == 1:
            return self._entries[0][0]

        indices = [i for i in range(len(self._entries)) if i != self._last_index]
        weights = [self._entries[i][1] for i in indices]
        idx = random.choices(indices, weights=weights, k=1)[0]
        self._last_index = idx
        return self._entries[idx][0]

    def get_filler_bytes(self) -> bytes | None:
        """Read a weighted-random filler file into bytes."""
        path = self.get_random_filler()
        if path is None:
            return None
        try:
            return path.read_bytes()
        except OSError:
            return None
