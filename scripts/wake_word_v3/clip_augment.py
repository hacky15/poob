"""Clipping augmentation for wake-word positive samples.

Three augmentation modes, each modeling a real production failure mode:

  head_clip(ms)       — VAD truncated the onset / packet loss at start of utterance.
                        "Hey Poob" → "ey Poob", "y Poob", "Poob".
  tail_clip(ms)       — Trailing silence cropped (typically benign) OR the
                        speaker is cut off ("Hey Poob, plyy ...").
  mid_dropout(ms,pos) — Brief mic dropout / network glitch in the middle of
                        the wake phrase ("Hey P[•]b", "He[•] Poob").

The user's 2026-05-13 production failures were dominated by head-clipped
audio that STT rendered as "A Poob" / "Pay Poob". Onset clipping is the
single highest-leverage missing augmentation per
``docs/research/wake-word-augmentation-2026.md``.

All operations work on raw int16 PCM at 16 kHz mono (the canonical
training format). No scipy/librosa dependency — pure byte-slice ops.
"""

from __future__ import annotations

import random
from typing import Iterator

from .audio_utils import (
    ms_to_bytes,
    read_wav_pcm,
    silent_pcm,
    write_wav_pcm,
)


# Conservative head-clip distribution. 30 ms is barely perceptible; 200 ms
# eats most of the "Hey" without losing the wake-word stem. Going beyond
# 200 ms loses too much of the "P" onset to be recoverable.
HEAD_CLIP_MS = (30, 60, 100, 150, 200)

# Tail-clip is mostly silence; the upper bound only matters for utterances
# that include trailing context ("Hey Poob, play music"). 50-250 ms span.
TAIL_CLIP_MS = (50, 100, 150, 200, 250)

# Mid-dropout simulates a packet-loss gap. Keep individual gaps short so
# the wake phrase remains recognizable; longer gaps fragment the audio
# beyond what production glitches actually produce.
MID_DROPOUT_MS = (30, 50, 70, 100, 150)


def head_clip(pcm: bytes, ms: float) -> bytes:
    """Drop ``ms`` milliseconds from the start of the PCM buffer."""
    cut = ms_to_bytes(ms)
    return pcm[cut:] if cut < len(pcm) else b""


def tail_clip(pcm: bytes, ms: float) -> bytes:
    """Drop ``ms`` milliseconds from the end of the PCM buffer."""
    cut = ms_to_bytes(ms)
    return pcm[:-cut] if cut and cut < len(pcm) else pcm


def mid_dropout(
    pcm: bytes, gap_ms: float, position_frac: float = 0.5,
) -> bytes:
    """Replace a ``gap_ms`` window inside the buffer with silence.

    ``position_frac`` (0..1) places the gap's start as a fraction of the
    total duration. 0.5 = right in the middle; 0.3 / 0.7 are good
    auxiliary picks for diversity.
    """
    total = len(pcm)
    gap_bytes = ms_to_bytes(gap_ms)
    if gap_bytes <= 0 or gap_bytes >= total:
        return pcm
    start = int(total * max(0.0, min(1.0, position_frac)))
    start = max(0, min(start, total - gap_bytes))
    return pcm[:start] + silent_pcm(gap_ms) + pcm[start + gap_bytes:]


def mid_cut(pcm: bytes, gap_ms: float, position_frac: float = 0.5) -> bytes:
    """Hard-cut ``gap_ms`` from the middle (gap collapsed, no silence).

    Distinct from ``mid_dropout`` which preserves duration. Hard-cut
    matches a packet-loss-with-resync scenario where the audio
    continues immediately on the other side.
    """
    total = len(pcm)
    gap_bytes = ms_to_bytes(gap_ms)
    if gap_bytes <= 0 or gap_bytes >= total:
        return pcm
    start = int(total * max(0.0, min(1.0, position_frac)))
    start = max(0, min(start, total - gap_bytes))
    return pcm[:start] + pcm[start + gap_bytes:]


def generate_variants(wav_bytes: bytes) -> Iterator[tuple[str, bytes]]:
    """Yield ``(tag, wav_bytes)`` for every clip variant of one input.

    Tags are filename-safe suffixes describing the augmentation, so the
    orchestrator can keep deterministic filenames + skip already-
    generated variants on resume.

    Variant set per input (~22 augmentations):
      - 5 head-clip lengths
      - 5 tail-clip lengths
      - 5 mid-dropout lengths at three positions each (15 variants)
      - 2 mid-cut variants (matching packet-loss-with-resync)
    """
    pcm = read_wav_pcm(wav_bytes)
    if pcm is None or len(pcm) < ms_to_bytes(200):
        return  # Sample too short to be useful for clipping.

    for ms in HEAD_CLIP_MS:
        out = head_clip(pcm, ms)
        if out:
            yield (f"head{int(ms)}", write_wav_pcm(out))

    for ms in TAIL_CLIP_MS:
        out = tail_clip(pcm, ms)
        if out:
            yield (f"tail{int(ms)}", write_wav_pcm(out))

    for ms in MID_DROPOUT_MS:
        for pos in (0.3, 0.5, 0.7):
            out = mid_dropout(pcm, ms, pos)
            if out:
                yield (f"middrop{int(ms)}p{int(pos * 10)}", write_wav_pcm(out))

    for ms in (50, 100):
        out = mid_cut(pcm, ms, 0.5)
        if out:
            yield (f"midcut{int(ms)}", write_wav_pcm(out))


def generate_random_subset(
    wav_bytes: bytes, n: int, seed: int = 0,
) -> Iterator[tuple[str, bytes]]:
    """Yield ``n`` randomly-sampled variants from the full augmentation
    space — cheaper if you don't want every variant per sample."""
    rng = random.Random(seed)
    all_variants = list(generate_variants(wav_bytes))
    if not all_variants:
        return
    if n >= len(all_variants):
        yield from all_variants
        return
    yield from rng.sample(all_variants, n)
