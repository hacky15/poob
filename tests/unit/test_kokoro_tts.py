"""Tests for KokoroTTS — local-inference text-to-speech provider.

The actual ``kokoro_onnx`` package is heavy (~350 MB model on disk) so
unit tests substitute fakes via the class's lazy import + lazy model
construction hooks. See ``docs/plans/voice-latency-phase3-kokoro-ship.md``.
"""

from __future__ import annotations

import io
import sys
import wave
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest

from poob.voice.tts import KokoroTTS, build_tts_cascade


# ---------------------------------------------------------------------------
# is_available() probe + caching
# ---------------------------------------------------------------------------

class TestIsAvailable:
    def test_returns_false_when_kokoro_onnx_missing(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Force the import to fail so is_available() reports unavailable.
        # We block both already-cached and fresh imports.
        monkeypatch.setitem(sys.modules, "kokoro_onnx", None)
        tts = KokoroTTS()
        assert tts.is_available() is False

    def test_returns_true_when_kokoro_onnx_present(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Inject a fake module so the lazy import succeeds.
        monkeypatch.setitem(sys.modules, "kokoro_onnx", MagicMock())
        tts = KokoroTTS()
        assert tts.is_available() is True

    def test_caches_after_first_check(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import_count = {"hits": 0}
        original_import = __import__

        def counting_import(name, *args, **kwargs):
            if name == "kokoro_onnx":
                import_count["hits"] += 1
            return original_import(name, *args, **kwargs)

        monkeypatch.setitem(sys.modules, "kokoro_onnx", MagicMock())
        monkeypatch.setattr("builtins.__import__", counting_import)

        tts = KokoroTTS()
        tts.is_available()
        tts.is_available()
        tts.is_available()
        assert import_count["hits"] == 1


# ---------------------------------------------------------------------------
# Name string includes the voice id so cascade logging is unambiguous
# ---------------------------------------------------------------------------

class TestName:
    def test_name_includes_voice_id(self) -> None:
        assert KokoroTTS(voice="af_heart").name == "kokoro:af_heart"
        assert KokoroTTS(voice="bf_emma").name == "kokoro:bf_emma"


# ---------------------------------------------------------------------------
# synthesize() — model call, WAV framing, error handling
# ---------------------------------------------------------------------------

class TestSynthesize:
    @pytest.mark.asyncio
    async def test_returns_wav_bytes_via_mocked_model(self) -> None:
        # Fake samples: 100 ms of 1 kHz tone at 24000 Hz sample rate.
        samples = np.sin(2 * np.pi * 1000 * np.arange(2400) / 24000).astype(np.float32)

        fake_model = MagicMock()
        fake_model.create = MagicMock(return_value=(samples, 24000))
        tts = KokoroTTS(voice="af_heart", speed=1.0)
        tts._model = fake_model  # bypass lazy construction

        wav_bytes = await tts.synthesize("hello world")

        # Parse it back as a WAV to confirm valid framing.
        with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
            assert wf.getnchannels() == 1
            assert wf.getsampwidth() == 2
            assert wf.getframerate() == 24000
            assert wf.getnframes() == 2400

    @pytest.mark.asyncio
    async def test_returns_empty_bytes_on_model_failure(self) -> None:
        fake_model = MagicMock()
        fake_model.create = MagicMock(side_effect=RuntimeError("kokoro crash"))
        tts = KokoroTTS()
        tts._model = fake_model

        result = await tts.synthesize("anything")
        assert result == b""

    @pytest.mark.asyncio
    async def test_uses_speed_param(self) -> None:
        samples = np.zeros(2400, dtype=np.float32)
        fake_model = MagicMock()
        fake_model.create = MagicMock(return_value=(samples, 24000))
        tts = KokoroTTS(voice="af_heart", speed=1.5)
        tts._model = fake_model

        await tts.synthesize("ping")

        # Assert the speed arg made it through.
        _args, kwargs = fake_model.create.call_args
        assert kwargs.get("speed") == 1.5
        assert kwargs.get("voice") == "af_heart"


# ---------------------------------------------------------------------------
# build_tts_cascade: pin the cascade order — Fenrir primary, Kokoro is the
# first fallback so a Google outage routes to a different failure mode
# (local, no network) rather than another cloud service.
# ---------------------------------------------------------------------------

class TestCascadeOrder:
    def test_default_cascade_is_google_kokoro_edge(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Force all three providers to report available so the cascade
        # surfaces every entry. Without this, network/key checks would
        # filter them out in a test environment.
        monkeypatch.setattr(
            "poob.voice.tts.GoogleCloudTTS.is_available", lambda self: True,
        )
        monkeypatch.setattr(
            "poob.voice.tts.EdgeTTS.is_available", lambda self: True,
        )
        monkeypatch.setattr(
            "poob.voice.tts.KokoroTTS.is_available", lambda self: True,
        )

        cascade = build_tts_cascade(
            preferred="google_tts",
            google_api_key="x",
            google_voice="en-US-Chirp3-HD-Fenrir",
        )

        # Class-name order pins the cascade — Fenrir primary, then local
        # Kokoro, then Edge as last-resort cloud.
        names = [type(p).__name__ for p in cascade]
        assert names == ["GoogleCloudTTS", "KokoroTTS", "EdgeTTS"]

    def test_kokoro_preferred_still_keeps_others_as_fallback(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # When operator opts into Kokoro, Google is the first fallback
        # (highest quality), then Edge as last resort.
        monkeypatch.setattr(
            "poob.voice.tts.GoogleCloudTTS.is_available", lambda self: True,
        )
        monkeypatch.setattr(
            "poob.voice.tts.EdgeTTS.is_available", lambda self: True,
        )
        monkeypatch.setattr(
            "poob.voice.tts.KokoroTTS.is_available", lambda self: True,
        )

        cascade = build_tts_cascade(
            preferred="kokoro",
            google_api_key="x",
        )

        names = [type(p).__name__ for p in cascade]
        # Kokoro first; remaining order preserves insertion-order
        # (Google before Edge).
        assert names[0] == "KokoroTTS"
        assert names == ["KokoroTTS", "GoogleCloudTTS", "EdgeTTS"]

    def test_unavailable_provider_drops_from_cascade(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Kokoro unavailable (e.g. dep missing on a stripped image) —
        # cascade should be [Google, Edge] with no gap.
        monkeypatch.setattr(
            "poob.voice.tts.GoogleCloudTTS.is_available", lambda self: True,
        )
        monkeypatch.setattr(
            "poob.voice.tts.EdgeTTS.is_available", lambda self: True,
        )
        monkeypatch.setattr(
            "poob.voice.tts.KokoroTTS.is_available", lambda self: False,
        )

        cascade = build_tts_cascade(
            preferred="google_tts", google_api_key="x",
        )

        names = [type(p).__name__ for p in cascade]
        assert names == ["GoogleCloudTTS", "EdgeTTS"]
