"""Tests for DualPipelineProcessor utterance-level dedup.

A re-triggered or Deepgram-resent identical transcript must not
double-fire the addressed callback (which would double-queue a
response). Scoped to addressed emits; passive context emits are not
deduped. See docs/plans/voice-pipeline-reliability.md (Issue 4).
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

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


# ---------------------------------------------------------------------------
# Zombie-stream recovery: a Deepgram socket reporting connected=True but
# delivering no transcripts must be force-reconnected after N consecutive
# wake-fired-but-no-transcript misses (passive reconnect only fires on
# connected=False, so it never self-heals). See
# docs/incidents/deepgram-zombie-stream-no-transcript.md.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_report_lost_transcript_recovers_at_threshold() -> None:
    mgr = DeepgramStreamManager("key")
    mgr.force_reconnect = AsyncMock()
    assert mgr._ZOMBIE_LOST_THRESHOLD == 2
    first = await mgr.report_lost_transcript(42)
    assert first is False
    mgr.force_reconnect.assert_not_awaited()
    second = await mgr.report_lost_transcript(42)
    assert second is True
    mgr.force_reconnect.assert_awaited_once_with(42)


@pytest.mark.asyncio
async def test_transcript_delivered_resets_miss_counter() -> None:
    mgr = DeepgramStreamManager("key")
    mgr.force_reconnect = AsyncMock()
    await mgr.report_lost_transcript(42)  # miss 1
    mgr.note_transcript_delivered(42)  # stream proved alive → reset
    recovered = await mgr.report_lost_transcript(42)  # miss 1 again, not 2
    assert recovered is False
    mgr.force_reconnect.assert_not_awaited()


@pytest.mark.asyncio
async def test_misses_are_per_user_isolated() -> None:
    mgr = DeepgramStreamManager("key")
    mgr.force_reconnect = AsyncMock()
    await mgr.report_lost_transcript(1)
    recovered = await mgr.report_lost_transcript(2)  # different user, count starts at 1
    assert recovered is False
    mgr.force_reconnect.assert_not_awaited()


@pytest.mark.asyncio
async def test_force_reconnect_closes_clears_throttle_and_counter() -> None:
    mgr = DeepgramStreamManager("key")
    mgr.close_user = AsyncMock()
    mgr._last_connect_time[42] = 123.0
    mgr._consecutive_lost[42] = 5
    await mgr.force_reconnect(42)
    mgr.close_user.assert_awaited_once_with(42)  # zombie stream torn down
    assert 42 not in mgr._last_connect_time  # throttle cleared → next frame reconnects now
    assert mgr._consecutive_lost[42] == 0  # counter reset


class _FakeWS:
    """Async-iterable that yields one Deepgram "Results" JSON message, then ends
    (mimics `async for msg in stream.ws` returning after a single frame)."""

    def __init__(self, transcript: str) -> None:
        self._transcript = transcript
        self._sent = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._sent:
            raise StopAsyncIteration
        self._sent = True
        import json

        return json.dumps(
            {
                "type": "Results",
                "is_final": True,
                "speech_final": True,
                "channel": {"alternatives": [{"transcript": self._transcript}]},
            }
        )


@pytest.mark.asyncio
async def test_listen_loop_does_not_reset_miss_counter_on_unrelated_content() -> None:
    """2026-07-02 regression: `_listen_loop` used to reset `_consecutive_lost` on
    ANY transcript, so a chronically-degraded stream's unrelated background
    content kept clearing the counter and it almost never reached
    `_ZOMBIE_LOST_THRESHOLD` (11/12 wake-fired misses for one user in prod, only
    1 auto-recovery all night). The counter must be reset ONLY by a genuine
    wake-fired success (`note_transcript_delivered`) or `force_reconnect` — not
    by this listener loop. Drives the REAL `_listen_loop` code path (not a
    reimplementation), so it would have caught the regression.
    See docs/incidents/deepgram-zombie-stream-no-transcript.md."""
    mgr = DeepgramStreamManager("key")
    stream = _UserStream()
    stream.ws = _FakeWS("just some background chatter")
    mgr._streams[42] = stream
    mgr._consecutive_lost[42] = 1  # simulate one prior wake-fired miss

    await mgr._listen_loop(42, stream)

    assert mgr.get_transcript(42)[0] == "just some background chatter"  # content still lands
    assert mgr._consecutive_lost[42] == 1, (
        "unrelated transcript content must NOT reset the zombie-miss counter"
    )


# ---------------------------------------------------------------------------
# Defect B (2026-07-02, found in the perfection-check adversarial audit of the
# fix above): _deferred_emit accepted the FIRST non-empty get_transcript()
# result within its 1.5s poll window with no check that it belonged to the
# wake-fired utterance. If the user spoke again during the wait, that LATER,
# unrelated utterance's content could be misattributed as an addressed command
# (bypassing the dual-gate) AND could falsely clear the zombie-miss counter --
# reopening the exact bug class Defect A's fix just closed, one layer removed.
# Fixed by pinning _deferred_emit to expected_seq (captured synchronously
# before scheduling) via get_transcript_for_utterance.
# ---------------------------------------------------------------------------


def test_get_transcript_for_utterance_none_once_stream_moves_on() -> None:
    mgr, stream = _mgr_with_stream(1)
    assert mgr.get_transcript_for_utterance(1, 0) == ("", False)  # still ours, no content yet
    mgr.begin_utterance(1)  # a NEW utterance starts (utterance_seq -> 1)
    assert mgr.get_transcript_for_utterance(1, 0) is None, (
        "moved to a newer utterance -- the pinned (seq=0) content will never arrive"
    )


def test_get_transcript_for_utterance_returns_matching_content() -> None:
    mgr, stream = _mgr_with_stream(1)
    expected = mgr.current_utterance_seq(1)  # 0
    _feed(stream, "hey poob play tiki tiki fong")
    assert mgr.get_transcript_for_utterance(1, expected)[0] == "hey poob play tiki tiki fong"


def _make_proc_with_real_deepgram(uid: int = 1) -> DualPipelineProcessor:
    proc = DualPipelineProcessor.__new__(DualPipelineProcessor)
    proc._deepgram = DeepgramStreamManager("key")
    proc._deepgram._streams[uid] = _UserStream()
    proc._on_addressed = MagicMock()
    proc._on_passive = MagicMock()
    proc._last_emitted = {}
    return proc


@pytest.mark.asyncio
async def test_deferred_emit_does_not_adopt_later_unrelated_utterance() -> None:
    proc = _make_proc_with_real_deepgram(1)
    stream = proc._deepgram._streams[1]
    expected_seq = proc._deepgram.current_utterance_seq(
        1
    )  # 0 -- pinned before anything else happens

    # Before our deferred content arrives, the user starts speaking again --
    # a genuinely new, unrelated utterance that DOES get transcribed in time.
    proc._deepgram.begin_utterance(1)  # utterance_seq -> 1
    _feed(stream, "completely unrelated thing said next")

    await proc._deferred_emit(1, "tester", time.monotonic(), expected_seq)

    proc._on_addressed.assert_not_called()  # unrelated content NOT executed as a command
    assert proc._deepgram._consecutive_lost.get(1, 0) == 1  # correctly counted as OUR miss
    # The unrelated utterance's content is untouched -- available for its OWN
    # (separate) emit, not swallowed by ours.
    assert proc._deepgram.get_transcript_for_utterance(1, 1)[0] == (
        "completely unrelated thing said next"
    )


@pytest.mark.asyncio
async def test_deferred_emit_still_delivers_when_seq_matches() -> None:
    """Regression guard: the pin must not break the normal, correct case —
    Deepgram delivers OUR utterance's content within the window."""
    proc = _make_proc_with_real_deepgram(1)
    stream = proc._deepgram._streams[1]
    expected_seq = proc._deepgram.current_utterance_seq(1)
    _feed(stream, "hey poob play tiki tiki fong")  # lands for the SAME utterance

    await proc._deferred_emit(1, "tester", time.monotonic(), expected_seq)

    proc._on_addressed.assert_called_once()
    assert proc._deepgram._consecutive_lost.get(1, 0) == 0  # proven healthy, not a miss


# ---------------------------------------------------------------------------
# Defect A — the double-emit SEAL (2026-06-11 double-queue). After our VAD
# emit+reset, Deepgram re-appends to the SAME still-open utterance rebuilt the
# phrase and double-fired. The seal (utterance_seq/transcript_seq/emitted_seq,
# bumped on OUR speech-start) suppresses the re-emit while letting a genuine
# re-request (user spoke again -> new utterance) through.
# ---------------------------------------------------------------------------


def _mgr_with_stream(uid: int = 1) -> tuple[DeepgramStreamManager, _UserStream]:
    mgr = DeepgramStreamManager("key")
    stream = _UserStream()
    mgr._streams[uid] = stream
    return mgr, stream


def _feed(stream: _UserStream, text: str, *, is_final: bool = True) -> None:
    """Mimic _listen_loop applying one Deepgram segment (new-utterance reset
    keyed on utterance_seq, then append/interim)."""
    if stream.utterance_seq != stream.transcript_seq:
        stream.transcript = ""
        stream.latest_interim = ""
        stream.transcript_seq = stream.utterance_seq
    if is_final:
        stream.transcript = f"{stream.transcript} {text}".strip() if stream.transcript else text
        stream.latest_interim = ""
    else:
        stream.latest_interim = text


def test_seal_suppresses_reaccumulation_but_allows_genuine_reissue() -> None:
    mgr, stream = _mgr_with_stream(1)

    # Utterance 1 — user says it once.
    mgr.begin_utterance(1)  # our speech-start
    _feed(stream, "hey poob play funny friends")
    assert mgr.get_transcript(1)[0] == "hey poob play funny friends"

    mgr.mark_emitted(1)  # VAD emit -> SEAL
    mgr.reset_transcript(1)

    # Deepgram re-appends to the SAME open utterance (no new speech-start) —
    # the re-accumulation that double-queued in prod.
    _feed(stream, "hey poob play funny friends")
    assert mgr.get_transcript(1)[0] == "", "re-accumulated same utterance must be sealed"

    # 14s later the user GENUINELY asks again -> new speech-start -> new utterance.
    mgr.begin_utterance(1)
    _feed(stream, "hey poob play funny friends")
    assert mgr.get_transcript(1)[0] == "hey poob play funny friends", (
        "a genuine re-request (user spoke again) must NOT be sealed"
    )


def test_seal_covers_the_latest_interim_fallback() -> None:
    """get_transcript falls back to latest_interim when transcript is empty;
    the seal must cover that path or the rebuilt phrase leaks via the interim
    (the exact original double-fire route)."""
    mgr, stream = _mgr_with_stream(1)
    mgr.begin_utterance(1)
    _feed(stream, "hey poob play x", is_final=False)  # interim only
    assert mgr.get_transcript(1)[0] == "hey poob play x"

    mgr.mark_emitted(1)
    mgr.reset_transcript(1)
    _feed(stream, "hey poob play x", is_final=False)  # interim re-arrives, same utterance
    assert mgr.get_transcript(1)[0] == "", "interim fallback must also be sealed"


def test_seal_first_utterance_is_never_sealed() -> None:
    """emitted_seq starts at -1 so the very first utterance always emits."""
    mgr, stream = _mgr_with_stream(7)
    mgr.begin_utterance(7)
    _feed(stream, "hey poob play first song")
    assert mgr.get_transcript(7)[0] == "hey poob play first song"
