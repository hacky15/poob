"""Tests for DualPipelineProcessor utterance-level dedup.

A re-triggered or Deepgram-resent identical transcript must not
double-fire the addressed callback (which would double-queue a
response). Scoped to addressed emits; passive context emits are not
deduped. See docs/plans/voice-pipeline-reliability.md (Issue 4).
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from poob.voice.dual_pipeline import DeepgramStreamManager, DualPipelineProcessor, _UserStream


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


# ---------------------------------------------------------------------------
# Deepgram reconnect buffering (Issue 3) — frames buffered while the stream is
# down are flushed in order on reconnect, instead of being dropped.
# ---------------------------------------------------------------------------

def _connected_stream() -> tuple[_UserStream, list[bytes]]:
    """A connected _UserStream whose ws.send records frames."""
    sent: list[bytes] = []
    s = _UserStream()
    s.connected = True
    ws = MagicMock()

    async def _send(data: bytes) -> None:
        sent.append(data)

    ws.send = _send
    s.ws = ws
    return s, sent


def test_buffer_pending_is_bounded() -> None:
    mgr = DeepgramStreamManager("key")
    for i in range(mgr._PENDING_MAX_FRAMES + 50):
        mgr._buffer_pending(1, bytes([i % 256]))
    assert len(mgr._pending_audio[1]) == mgr._PENDING_MAX_FRAMES  # oldest dropped


@pytest.mark.asyncio
async def test_flush_pending_sends_in_order_and_clears() -> None:
    mgr = DeepgramStreamManager("key")
    mgr._buffer_pending(1, b"a")
    mgr._buffer_pending(1, b"b")
    mgr._buffer_pending(1, b"c")
    stream, sent = _connected_stream()
    await mgr._flush_pending(1, stream)
    assert sent == [b"a", b"b", b"c"]
    assert len(mgr._pending_audio[1]) == 0


@pytest.mark.asyncio
async def test_flush_pending_rebuffers_on_failure() -> None:
    mgr = DeepgramStreamManager("key")
    mgr._buffer_pending(1, b"a")
    mgr._buffer_pending(1, b"b")
    stream = _UserStream()
    stream.connected = True
    ws = MagicMock()

    async def _boom(data: bytes) -> None:
        raise RuntimeError("ws closed")

    ws.send = _boom
    stream.ws = ws
    await mgr._flush_pending(1, stream)
    assert stream.connected is False
    # The frame that failed stays buffered for the next reconnect.
    assert list(mgr._pending_audio[1]) == [b"a", b"b"]


@pytest.mark.asyncio
async def test_send_audio_connected_drains_stragglers_then_current() -> None:
    mgr = DeepgramStreamManager("key")
    stream, sent = _connected_stream()
    mgr._streams[1] = stream
    # A straggler buffered from a prior down-window.
    mgr._buffer_pending(1, b"old")
    await mgr.send_audio(1, b"new")
    assert sent == [b"old", b"new"]  # straggler first, then current
    assert len(mgr._pending_audio[1]) == 0


@pytest.mark.asyncio
async def test_send_audio_connected_failure_buffers_current() -> None:
    mgr = DeepgramStreamManager("key")
    stream = _UserStream()
    stream.connected = True
    ws = MagicMock()

    async def _boom(data: bytes) -> None:
        raise RuntimeError("ws closed")

    ws.send = _boom
    stream.ws = ws
    mgr._streams[1] = stream
    await mgr.send_audio(1, b"frame")
    assert stream.connected is False
    assert list(mgr._pending_audio[1]) == [b"frame"]  # not lost
