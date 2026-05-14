"""Audio utilities for wake-word training data generation.

Pure stdlib + ffmpeg shell-outs — no scipy/librosa dependency so the
script can run inside the Docker GHA runner image without dragging in
~300 MB of scientific Python.

All WAVs the pipeline produces are **16 kHz mono 16-bit PCM** — the
exact format OpenWakeWord's feature extractor consumes. Any conversion
from MP3 (Edge TTS output) routes through ``mp3_to_wav_16k_mono``.
"""

from __future__ import annotations

import os
import struct
import subprocess
import tempfile
from typing import Iterable


WAV_HEADER_SIZE = 44
SAMPLE_RATE = 16000
SAMPLE_WIDTH_BYTES = 2  # 16-bit PCM
CHANNELS = 1


def mp3_to_wav_16k_mono(mp3_bytes: bytes) -> bytes | None:
    """Convert MP3 bytes to 16 kHz mono WAV via ffmpeg. Returns ``None`` on
    failure so the caller can skip cleanly instead of inflating the
    corpus with corrupted samples."""
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp_in:
        tmp_in.write(mp3_bytes)
        tmp_in_path = tmp_in.name
    tmp_out_path = tmp_in_path.replace(".mp3", ".wav")
    try:
        subprocess.run(
            [
                "ffmpeg", "-y", "-i", tmp_in_path,
                "-ar", str(SAMPLE_RATE), "-ac", str(CHANNELS),
                "-sample_fmt", "s16",
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


def read_wav_pcm(wav_bytes: bytes) -> bytes | None:
    """Return the raw int16 PCM frames after the WAV header.

    Assumes the canonical 16 kHz / mono / 16-bit format. Returns
    ``None`` for malformed inputs."""
    if len(wav_bytes) <= WAV_HEADER_SIZE:
        return None
    # Quick header sanity check — RIFF + WAVE markers in the right slots.
    if wav_bytes[:4] != b"RIFF" or wav_bytes[8:12] != b"WAVE":
        return None
    return wav_bytes[WAV_HEADER_SIZE:]


def write_wav_pcm(pcm: bytes) -> bytes:
    """Wrap raw int16 PCM frames in a 16 kHz mono WAV header."""
    n_samples = len(pcm) // SAMPLE_WIDTH_BYTES
    byte_rate = SAMPLE_RATE * CHANNELS * SAMPLE_WIDTH_BYTES
    block_align = CHANNELS * SAMPLE_WIDTH_BYTES
    data_size = len(pcm)
    riff_size = 36 + data_size
    header = b"".join([
        b"RIFF",
        struct.pack("<I", riff_size),
        b"WAVE",
        b"fmt ",
        struct.pack("<I", 16),         # fmt chunk size
        struct.pack("<H", 1),          # PCM format
        struct.pack("<H", CHANNELS),
        struct.pack("<I", SAMPLE_RATE),
        struct.pack("<I", byte_rate),
        struct.pack("<H", block_align),
        struct.pack("<H", 16),         # bits per sample
        b"data",
        struct.pack("<I", data_size),
    ])
    return header + pcm


def ms_to_samples(ms: float) -> int:
    """Milliseconds → 16 kHz int16 sample count (NOT byte count)."""
    return int(ms * SAMPLE_RATE / 1000)


def ms_to_bytes(ms: float) -> int:
    """Milliseconds → byte offset in 16-bit PCM."""
    return ms_to_samples(ms) * SAMPLE_WIDTH_BYTES


def silent_pcm(ms: float) -> bytes:
    """Generate ``ms`` of silent 16-bit PCM (for mid-cut replacement)."""
    return b"\x00\x00" * ms_to_samples(ms)


def list_wavs(directory: str) -> list[str]:
    """Sorted .wav filenames in a directory; empty list if missing."""
    if not os.path.isdir(directory):
        return []
    return sorted(f for f in os.listdir(directory) if f.endswith(".wav"))


def chunk(iterable: Iterable, size: int):
    """Yield successive ``size``-sized chunks from ``iterable``."""
    batch: list = []
    for item in iterable:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch
