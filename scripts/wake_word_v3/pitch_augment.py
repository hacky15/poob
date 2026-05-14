"""Pitch + speed perturbation for wake-word positives.

The user's directive (2026-05-13): people say "Hey Poob" at wildly
different pitches and speeds — high-voiced fast speakers, low-voiced
slow speakers, plus everything in between. The wake model needs to
have heard all of them.

Edge TTS already covers 8 speaking rates (-30% to +30%) and 47
distinct voice identities (mix of male / female / age / region). That
gets us most of the speaker-pitch diversity for free. This module
adds the **non-Edge-covered** axes:

  1. **Independent pitch shift** — same voice, same speed, different
     pitch. Useful for simulating microphone tonal coloring (cheap
     gaming headsets vs studio mic vs phone speaker), and for the
     "chipmunk fast" failure mode where a normally-pitched voice gets
     pitch-shifted by an Opus codec artifact.
  2. **Pitch + speed coupled** (chipmunk / deep) — asetrate trick from
     the Toob/Boob TTS filter chain. Useful because in real production,
     fast-talking high-voiced users get further pitched up by the
     ``rate=+20%`` Edge TTS path; we want training data that covers the
     compounded effect.
  3. **Speed-invariant pitch shift** — atempo correction layered on top
     of asetrate so the audio duration stays the same but pitch
     changes. Models the "different speaker, same speech rate" case
     that pure asetrate misses.

Six presets, each one filter-chain-string applied via a single ffmpeg
subprocess on a 16 kHz mono 16-bit PCM input. No scipy/librosa. The
chains use the same ``asetrate / aresample / atempo`` primitives we
already use in ``voice/session.py``'s Toob/Boob persona filters
([[toob-voice-filter-chain]] / [[boob-music-wrap-variant]]) so the
operator can intuit them.

Math: 6 pitch presets applied to a FRACTION of base WAVs (default
0.25, operator-tunable). At full augmentation that's
``169k × 6 × 0.25 = ~253k`` additional pitched positives before clip
augmentation. Combined with clip augmentation (27× per WAV) the
pitched + clipped tail adds ~6.8M positives at the full settings.
"""

from __future__ import annotations

import os
import random
import subprocess
import tempfile
from typing import Iterator

from .audio_utils import SAMPLE_RATE


# Pitch / speed perturbation presets.
#
# All presets operate on 16 kHz mono inputs (the canonical training
# format). ``asetrate=Nk`` reinterprets the source at a different rate,
# which simultaneously raises (or lowers) BOTH pitch and tempo by the
# factor N/SAMPLE_RATE. ``aresample=16000`` brings the playable rate
# back. ``atempo=X`` adjusts tempo independently (1.0 = unchanged).
#
# Examples for SAMPLE_RATE = 16000:
#   asetrate=19200,aresample=16000        → pitch + tempo × 1.20 (chipmunk-fast)
#   asetrate=12800,aresample=16000        → pitch + tempo × 0.80 (deep-slow)
#   asetrate=19200,aresample=16000,atempo=0.833 → pitch × 1.20, tempo × 1.0
#                                                 (high-pitched, normal speed)
#   asetrate=12800,aresample=16000,atempo=1.25  → pitch × 0.80, tempo × 1.0
#                                                 (deep, normal speed)
PITCH_PRESETS: dict[str, str] = {
    # Chipmunk-fast: pitch up + speed up. Matches "fast high-voiced user".
    "high_fast": f"asetrate={int(SAMPLE_RATE * 1.20)},aresample={SAMPLE_RATE}",
    # Even more extreme — small minority of speakers actually sound like this
    # (kids, rapid-fire excited high-voiced adults).
    "very_high_fast": f"asetrate={int(SAMPLE_RATE * 1.30)},aresample={SAMPLE_RATE}",
    # Deep-slow: pitch down + speed down. Drawled low-voiced speakers.
    "low_slow": f"asetrate={int(SAMPLE_RATE * 0.80)},aresample={SAMPLE_RATE}",
    # Extreme deep — voice-deepening Discord effects + low bass-heavy mics.
    "very_low_slow": f"asetrate={int(SAMPLE_RATE * 0.70)},aresample={SAMPLE_RATE}",
    # Speed-invariant pitch up: simulates a different speaker at the same
    # speaking rate (different mic / different gender / different age).
    "high_same_speed": (
        f"asetrate={int(SAMPLE_RATE * 1.20)},"
        f"aresample={SAMPLE_RATE},atempo=0.833"
    ),
    # Speed-invariant pitch down — counterpart of the above.
    "low_same_speed": (
        f"asetrate={int(SAMPLE_RATE * 0.80)},"
        f"aresample={SAMPLE_RATE},atempo=1.25"
    ),
}

# Names listed in the order we usually want them applied — high-leverage
# (chipmunk-fast / deep-slow couplings) first, speed-invariant variants
# second. Operator can subset via the orchestrator's
# ``pitch_presets`` config.
DEFAULT_PRESETS: tuple[str, ...] = (
    "high_fast",
    "very_high_fast",
    "low_slow",
    "very_low_slow",
    "high_same_speed",
    "low_same_speed",
)


def apply_pitch(wav_bytes: bytes, filter_chain: str) -> bytes | None:
    """Apply an ffmpeg ``-af`` chain to a WAV buffer.

    Reads + writes through temp files (ffmpeg doesn't reliably take
    raw WAV bytes on stdin across versions). Returns ``None`` on
    ffmpeg failure so the caller can skip cleanly rather than write a
    corrupt file.
    """
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp_in:
        tmp_in.write(wav_bytes)
        tmp_in_path = tmp_in.name
    tmp_out_path = tmp_in_path.replace(".wav", "_pitched.wav")
    try:
        subprocess.run(
            [
                "ffmpeg", "-y", "-i", tmp_in_path,
                "-af", filter_chain,
                "-ar", str(SAMPLE_RATE), "-ac", "1", "-sample_fmt", "s16",
                tmp_out_path,
            ],
            capture_output=True, timeout=15,
        )
        if os.path.exists(tmp_out_path):
            with open(tmp_out_path, "rb") as f:
                return f.read()
    except (subprocess.SubprocessError, OSError):
        pass
    finally:
        for p in (tmp_in_path, tmp_out_path):
            try:
                os.unlink(p)
            except OSError:
                pass
    return None


def generate_variants(
    wav_bytes: bytes,
    presets: tuple[str, ...] = DEFAULT_PRESETS,
) -> Iterator[tuple[str, bytes]]:
    """Yield ``(tag, wav_bytes)`` for each pitch preset applied to one input.

    Tags are filename-safe (lowercase, underscore-only) so the
    orchestrator can build deterministic filenames + skip already-
    generated variants on resume.
    """
    for name in presets:
        chain = PITCH_PRESETS.get(name)
        if chain is None:
            continue
        out = apply_pitch(wav_bytes, chain)
        if out:
            yield (f"pitch_{name}", out)


def generate_random_subset(
    wav_bytes: bytes, n: int, seed: int = 0,
    presets: tuple[str, ...] = DEFAULT_PRESETS,
) -> Iterator[tuple[str, bytes]]:
    """Yield ``n`` randomly-sampled pitch variants from the preset list."""
    rng = random.Random(seed)
    candidate_names = list(presets)
    if n >= len(candidate_names):
        chosen = candidate_names
    else:
        chosen = rng.sample(candidate_names, n)
    for name in chosen:
        chain = PITCH_PRESETS.get(name)
        if chain is None:
            continue
        out = apply_pitch(wav_bytes, chain)
        if out:
            yield (f"pitch_{name}", out)
