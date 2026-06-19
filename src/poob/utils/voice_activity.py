"""Process-wide voice-activity beacon.

The poob container runs the real-time voice pipeline AND the marketplace
patrol scanner in a single process on a CPU-only box. Under multi-user voice
the scanner's chromium cycles starve voice inference (OpenWakeWord, Deepgram,
TTS), causing multi-second wake latency and dropped requests.

This beacon lets the scanner back off while voice is active WITHOUT coupling
the two subsystems: the voice pipeline calls ``mark_active()`` from its audio
hot path; the patrol scheduler calls ``is_active()`` before each cycle. It is a
single module-level monotonic timestamp — deliberately the simplest possible
cross-subsystem signal (no objects, no callbacks, no locks; a single float
write is atomic under the GIL).

See docs/decisions/patrol-backoff-during-voice.md.
"""

from __future__ import annotations

import time

# Monotonic timestamp of the most recent voice-pipeline audio activity.
# 0.0 means "never active this process" (the scanner runs unthrottled).
_last_active_monotonic: float = 0.0


def mark_active(ts: float | None = None) -> None:
    """Record voice-pipeline activity. Cheap enough for the per-frame hot path.

    Args:
        ts: A ``time.monotonic()`` value to record (callers in the audio path
            already have one — pass it to avoid a second syscall). Defaults to
            ``time.monotonic()`` now.
    """
    global _last_active_monotonic
    _last_active_monotonic = ts if ts is not None else time.monotonic()


def seconds_since_active() -> float:
    """Seconds since the last voice activity. ``inf`` if never active."""
    if _last_active_monotonic <= 0.0:
        return float("inf")
    return max(0.0, time.monotonic() - _last_active_monotonic)


def is_active(window_s: float) -> bool:
    """True if the voice pipeline processed audio within ``window_s`` seconds."""
    return seconds_since_active() <= window_s


def reset() -> None:
    """Clear the beacon (tests / clean-shutdown only)."""
    global _last_active_monotonic
    _last_active_monotonic = 0.0
