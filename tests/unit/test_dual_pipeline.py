"""Tests for DualPipelineProcessor utterance-level dedup.

A re-triggered or Deepgram-resent identical transcript must not
double-fire the addressed callback (which would double-queue a
response). Scoped to addressed emits; passive context emits are not
deduped. See docs/plans/voice-pipeline-reliability.md (Issue 4).
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

from poob.voice.dual_pipeline import DualPipelineProcessor


def _make_proc() -> DualPipelineProcessor:
    """Build a processor without the real Deepgram/wake init (mirrors the
    __new__ pattern other dual_pipeline internals tolerate)."""
    proc = DualPipelineProcessor.__new__(DualPipelineProcessor)
    proc._on_addressed = MagicMock()
    proc._on_passive = MagicMock()
    proc._deepgram = MagicMock()  # reset_transcript()
    proc._last_emitted = {}
    return proc


def _emit(proc: DualPipelineProcessor, uid: int, text: str, addressed: bool = True) -> None:
    proc._do_emit(uid, "tester", time.monotonic(), addressed, text)


def test_duplicate_addressed_utterance_suppressed() -> None:
    proc = _make_proc()
    _emit(proc, 1, "Hey Poob play tiki tiki fong")
    _emit(proc, 1, "Hey Poob play tiki tiki fong")  # identical, within window
    assert proc._on_addressed.call_count == 1


def test_duplicate_normalizes_case_and_whitespace() -> None:
    proc = _make_proc()
    _emit(proc, 1, "Hey Poob play tiki tiki fong")
    _emit(proc, 1, "  hey   poob   PLAY tiki tiki fong ")  # same after normalize
    assert proc._on_addressed.call_count == 1


def test_distinct_transcripts_both_fire() -> None:
    proc = _make_proc()
    _emit(proc, 1, "Hey Poob play tiki tiki fong")
    _emit(proc, 1, "Hey Poob what's the score")
    assert proc._on_addressed.call_count == 2


def test_repeat_outside_window_fires_again() -> None:
    proc = _make_proc()
    # Seed a prior identical emit well outside the 8s window.
    proc._last_emitted[1] = ("hey poob play tiki tiki fong", time.monotonic() - 100.0)
    _emit(proc, 1, "Hey Poob play tiki tiki fong")
    assert proc._on_addressed.call_count == 1  # genuine repeat after window is allowed


def test_per_user_isolation() -> None:
    proc = _make_proc()
    _emit(proc, 1, "Hey Poob play tiki tiki fong")
    _emit(proc, 2, "Hey Poob play tiki tiki fong")  # different user, same text
    assert proc._on_addressed.call_count == 2


def test_passive_emits_not_deduped() -> None:
    proc = _make_proc()
    _emit(proc, 1, "just chatting here", addressed=False)
    _emit(proc, 1, "just chatting here", addressed=False)
    assert proc._on_passive.call_count == 2
    proc._on_addressed.assert_not_called()
