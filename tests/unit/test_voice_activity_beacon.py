"""Tests for the process-wide voice-activity beacon + patrol VC-backoff.

The poob container runs the voice pipeline AND the marketplace scanner in one
process on a CPU-only box. Under multi-user VC the scanner's chromium cycles
starve real-time voice inference (load hit 11.7 on 4 cores; speech_to_wake
ballooned to ~4s, Deepgram zombied). Fix: the voice pipeline marks activity as
it processes audio; the patrol scheduler backs off a cycle while voice is
recently active, so voice wins the shared CPU. Decoupled via a neutral
module-level beacon (neither subsystem references the other).

See docs/decisions/patrol-backoff-during-voice.md.
"""

from __future__ import annotations

import inspect

from poob.utils import voice_activity as va


def setup_function() -> None:
    va.reset()  # isolate tests from each other / prior process state


def test_inactive_by_default() -> None:
    assert va.is_active(120.0) is False
    assert va.seconds_since_active() == float("inf")


def test_mark_active_makes_it_active() -> None:
    va.mark_active()
    assert va.is_active(120.0) is True
    assert va.seconds_since_active() < 1.0


def test_window_expiry() -> None:
    # Mark active 200s in the past via an explicit monotonic timestamp.
    import time
    va.mark_active(time.monotonic() - 200.0)
    assert va.is_active(120.0) is False  # outside the 2-min window
    assert va.is_active(300.0) is True   # inside a 5-min window


def test_mark_active_accepts_explicit_timestamp() -> None:
    import time
    now = time.monotonic()
    va.mark_active(now)
    # ~0s since active
    assert va.seconds_since_active() < 1.0


# --- wiring contracts (grep-as-test): lock the two integration points ---

def test_voice_pipeline_marks_activity() -> None:
    """The audio hot path must mark the beacon, else the scanner can't know
    voice is busy."""
    from poob.voice.dual_pipeline import DualPipelineProcessor
    src = inspect.getsource(DualPipelineProcessor.process_audio_frame)
    assert "mark_active" in src, (
        "process_audio_frame must mark the voice-activity beacon — see "
        "docs/decisions/patrol-backoff-during-voice.md"
    )


def test_patrol_scheduler_checks_beacon() -> None:
    """The patrol loop must consult the beacon to back off during voice."""
    from poob.scanner.patrol_scheduler import PatrolScheduler
    src = inspect.getsource(PatrolScheduler._loop)
    assert "_should_skip_for_voice" in src, (
        "patrol _loop must skip cycles while voice is active — see "
        "docs/decisions/patrol-backoff-during-voice.md"
    )


# --- behavioral: the skip decision itself ---

def _scheduler(skip: bool, window: float = 120.0):
    from poob.scanner.patrol_scheduler import PatrolScheduler
    s = PatrolScheduler.__new__(PatrolScheduler)
    s._skip_during_voice = skip
    s._voice_activity_window_s = window
    return s


def test_skips_cycle_when_voice_active() -> None:
    va.mark_active()
    assert _scheduler(skip=True)._should_skip_for_voice() is True


def test_runs_cycle_when_voice_idle() -> None:
    va.reset()  # no voice activity
    assert _scheduler(skip=True)._should_skip_for_voice() is False


def test_runs_cycle_when_voice_active_but_window_expired() -> None:
    import time
    va.mark_active(time.monotonic() - 300.0)  # 5 min ago
    assert _scheduler(skip=True, window=120.0)._should_skip_for_voice() is False


def test_disabled_flag_never_skips_even_with_active_voice() -> None:
    """patrol_skip_during_voice=False must fully restore the old behavior —
    no functionality lost, one config flip to revert."""
    va.mark_active()
    assert _scheduler(skip=False)._should_skip_for_voice() is False
