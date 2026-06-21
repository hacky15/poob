"""Tests for voice-pipeline cold-start prewarm + wake-gate instrumentation.

Incident 2026-06-17: every voice model loaded lazily AFTER join — Deepgram
(+21s), OpenWakeWord (+24s), Model2Vec (+50s) — leaving a ~25-50s cold window
where wake/address detection wasn't ready and requests were silently dropped
(no transcript, no log). Users experienced "Poob isn't handling my voice
requests, and when it finally did there was great delay."

Fix: VoiceSession prewarms OpenWakeWord + Model2Vec in a background thread on
join (non-blocking — the loads are sync and were deferred to avoid stalling
the join). Plus a "Wake gate decision" log surfacing the OWW peak + both gate
signals, so warm-window wake-misses are debuggable instead of a bare
wake_word=False.

See docs/incidents/voice-pipeline-cold-start-drops-requests.md.
"""

from __future__ import annotations

import inspect

import pytest

from poob.voice.address_detector import MultiSignalAddressDetector
from poob.voice.dual_pipeline import DualPipelineProcessor, WakeWordDetector
from poob.voice.session import VoiceSession


def test_wake_detector_prewarm_loads_model(mocker) -> None:
    d = WakeWordDetector(model_path=None, threshold=0.7)
    m = mocker.patch.object(d, "_ensure_model")
    d.prewarm()
    m.assert_called_once()


def test_ensure_model_is_thread_safe_single_construction(mocker) -> None:
    """Concurrent first-frames (one thread per user) must not race the OWW lazy
    init — the model is constructed exactly once and nothing raises. Regression
    for the 'partially initialized module ... circular import' crash seen in
    prod 2026-06-21 under multi-user VC after a restart.

    Injects a fake ``openwakeword.model`` so the test runs without the package
    (it has no 3.12+ wheels and isn't installed for local dev)."""
    import sys
    import threading
    import time
    import types

    count = {"n": 0}
    count_lock = threading.Lock()

    class _FakeModel:
        def __init__(self, *args, **kwargs) -> None:
            with count_lock:
                count["n"] += 1
            time.sleep(0.02)  # widen the window — an unguarded init would double-construct

    fake = types.ModuleType("openwakeword.model")
    fake.Model = _FakeModel  # type: ignore[attr-defined]
    mocker.patch.dict(
        sys.modules,
        {"openwakeword": types.ModuleType("openwakeword"), "openwakeword.model": fake},
    )

    d = WakeWordDetector(model_path=None, threshold=0.7)
    errors: list[Exception] = []

    def worker() -> None:
        try:
            d._ensure_model()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"concurrent init raised: {errors}"
    assert count["n"] == 1, f"model constructed {count['n']}x — the init lock failed"
    assert d._model is not None


def test_wake_detector_peak_score_tracks_and_resets() -> None:
    d = WakeWordDetector(model_path=None, threshold=0.7)
    assert d.peak_score(123) == 0.0
    d._peak_scores[123] = 0.55  # simulate a sub-threshold peak this utterance
    assert d.peak_score(123) == 0.55
    d.reset_user(123)
    assert d.peak_score(123) == 0.0, "peak must reset between utterances"


def test_address_detector_prewarm_loads_model(mocker) -> None:
    a = MultiSignalAddressDetector()
    m = mocker.patch.object(a, "_ensure_model")
    a.prewarm()
    m.assert_called_once()


def test_dual_pipeline_prewarm_warms_wake_detector(mocker) -> None:
    p = DualPipelineProcessor.__new__(DualPipelineProcessor)
    p._wake_detector = mocker.MagicMock()
    p.prewarm()
    p._wake_detector.prewarm.assert_called_once()


@pytest.mark.asyncio
async def test_voice_session_prewarms_both_models(mocker) -> None:
    sess = VoiceSession.__new__(VoiceSession)
    sess._guild_id = 1
    sess._dual_pipeline = mocker.MagicMock()
    sess._address_detector = mocker.MagicMock()

    await sess._prewarm_models()

    sess._dual_pipeline.prewarm.assert_called_once()
    sess._address_detector.prewarm.assert_called_once()


@pytest.mark.asyncio
async def test_voice_session_prewarm_handles_no_dual_pipeline(mocker) -> None:
    """Energy-VAD-only sessions have no dual pipeline — prewarm must still warm
    the address detector and not crash."""
    sess = VoiceSession.__new__(VoiceSession)
    sess._guild_id = 1
    sess._dual_pipeline = None
    sess._address_detector = mocker.MagicMock()

    await sess._prewarm_models()  # must not raise

    sess._address_detector.prewarm.assert_called_once()


def test_session_init_kicks_off_prewarm() -> None:
    """Grep-as-test: the join path must schedule the prewarm, else the cold
    window returns."""
    src = inspect.getsource(VoiceSession.__init__)
    assert "_prewarm_models" in src, (
        "VoiceSession.__init__ must schedule _prewarm_models on join — see "
        "docs/incidents/voice-pipeline-cold-start-drops-requests.md"
    )


def test_wake_gate_decision_is_instrumented() -> None:
    """Grep-as-test: the dual-gate must log WHY it decided (signals + OWW peak),
    so wake-misses are diagnosable instead of a bare wake_word=False."""
    src = inspect.getsource(DualPipelineProcessor._emit_utterance_locked)
    assert "Wake gate decision" in src
    assert "oww_peak" in src and "text_match" in src and "audio_match" in src
