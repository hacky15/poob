"""Regression: the stale-buffer check must never flood the log.

2026-06-22 incident: session cleanup nulls `_dual_pipeline`, but an orphaned
sink's stale-check callback kept dereferencing it 10x/sec → `AttributeError:
'NoneType' object has no attribute 'check_stale_buffers'` → 59,247 tracebacks in
5h, starving the audio threads and crippling the whole bot (2 responses in 5h).

Two guards: the callback no-ops when the pipeline is gone (voice_cog), and the
sink throttles any recurring stale-check error to 1/30s. See
docs/incidents/stale-check-none-flood.
"""

from __future__ import annotations

import poob.voice.realtime_sink as rs


def _bare_sink(on_stale_check) -> rs.RealtimeAudioSink:
    # __new__ to skip the discord Sink base + the background thread; we drive
    # _check_stale_buffers directly.
    sink = rs.RealtimeAudioSink.__new__(rs.RealtimeAudioSink)
    sink._on_stale_check = on_stale_check
    sink._get_buffers = None
    sink._last_err_log = 0.0
    return sink


def test_recurring_stale_check_error_is_throttled(mocker) -> None:
    spy = mocker.patch.object(rs.logger, "exception")

    def boom() -> None:
        raise AttributeError("'NoneType' object has no attribute 'check_stale_buffers'")

    sink = _bare_sink(boom)
    for _ in range(200):  # the loop would do this in ~20s at 10x/sec
        sink._check_stale_buffers()

    # 200 identical failures collapse to a single log line (throttled), not 200.
    assert spy.call_count == 1


def test_noop_callback_does_not_log(mocker) -> None:
    """A None-safe callback (pipeline gone → no-op) raises nothing, logs nothing."""
    spy = mocker.patch.object(rs.logger, "exception")
    sink = _bare_sink(lambda: None)
    for _ in range(50):
        sink._check_stale_buffers()
    assert spy.call_count == 0
