"""Tests for the shared-model Silero VAD refactor.

The pre-refactor design constructed a fresh ``SileroVADProcessor`` per
``SpeechDetector`` because ``VoiceSession`` never passed a shared
``vad_processor=`` kwarg. Each new voice-channel user paid the
1-2 s ONNX load + warmup, stacking into a multi-second connect stall in
multi-user channels — the reason Phase 1 was disabled. See
``docs/plans/voice-latency-phase1-silero-reenable.md`` for the design.

The refactor adds:
  - ``SileroVADProcessor.process_frame_for_user(user_id, pcm_bytes)`` —
    a new entry point that maintains per-user ring buffer + per-user
    saved Silero LSTM state (via ``model._state`` / ``model._context``
    snapshot+restore) so the model is shared across users without
    bleeding acoustic context.
  - Lazy model load — first call to ``process_frame_for_user`` triggers
    the load; the constructor itself does NOT touch the ONNX runtime.
"""

from __future__ import annotations

import collections
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Shared instance + lazy load
# ---------------------------------------------------------------------------

class TestSharedInstanceLazyLoad:
    def test_constructor_does_not_load_model(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Phase 1's blocker was that ``SileroVADProcessor()`` blocked for
        ~1-2 s on the ONNX load + warmup at construction time. After the
        refactor, the model load is deferred until the first frame so
        connect-time isn't stalled by N users joining at once.
        """
        load_calls = {"hits": 0}

        def counting_load(onnx: bool = True):
            load_calls["hits"] += 1
            return MagicMock(_state=MagicMock(), _context=MagicMock())

        monkeypatch.setattr(
            "poob.voice.silero_vad.load_silero_vad", counting_load,
        )

        from poob.voice.silero_vad import SileroVADProcessor

        proc = SileroVADProcessor()
        assert load_calls["hits"] == 0, (
            "Construction must NOT load the model — load is deferred until "
            "first frame to avoid the multi-user join stall."
        )
        # Sanity: the processor exists
        assert proc is not None

    def test_first_frame_loads_model_once(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The first call to ``process_frame_for_user`` triggers the load.
        Subsequent calls (for the same or different users) MUST NOT re-load.
        """
        load_calls = {"hits": 0}
        fake_model = _make_fake_model()

        def counting_load(onnx: bool = True):
            load_calls["hits"] += 1
            return fake_model

        monkeypatch.setattr(
            "poob.voice.silero_vad.load_silero_vad", counting_load,
        )

        from poob.voice.silero_vad import SileroVADProcessor

        proc = SileroVADProcessor()
        # Send enough silence-frame audio for two users to trigger a few
        # inference passes each. The exact return value doesn't matter
        # for the load-count assertion.
        silence_frame = b"\x00" * 3840
        for _ in range(20):  # enough frames to cross 32 ms chunk boundary
            proc.process_frame_for_user(user_id=100, pcm_bytes=silence_frame)
            proc.process_frame_for_user(user_id=200, pcm_bytes=silence_frame)

        assert load_calls["hits"] == 1, (
            "Model must load exactly once across all users + all frames."
        )


# ---------------------------------------------------------------------------
# Per-user state isolation
# ---------------------------------------------------------------------------

class TestPerUserStateIsolation:
    def test_per_user_buffers_do_not_bleed(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """User A's partial-chunk audio (less than 512 samples accumulated)
        must NOT spill into user B's probabilities. Each user has their
        own ring buffer.
        """
        fake_model = _make_fake_model()
        monkeypatch.setattr(
            "poob.voice.silero_vad.load_silero_vad", lambda onnx=True: fake_model,
        )

        from poob.voice.silero_vad import SileroVADProcessor

        proc = SileroVADProcessor()

        # Each Discord frame yields 320 samples after resampling. Silero
        # chunks at 512. So one A-frame = 320 in A's buffer, no chunk
        # yet; one B-frame = 320 in B's buffer; both users still pending.
        single_frame = b"\x00" * 3840
        probs_a = proc.process_frame_for_user(user_id=1, pcm_bytes=single_frame)
        probs_b = proc.process_frame_for_user(user_id=2, pcm_bytes=single_frame)
        assert probs_a == []
        assert probs_b == []

        # A second frame for A pushes it over 512 → one chunk emitted
        # for A specifically. B should still have 320 samples buffered
        # (not 640 — that would mean B inherited A's leftover).
        probs_a2 = proc.process_frame_for_user(user_id=1, pcm_bytes=single_frame)
        assert len(probs_a2) == 1, "A should produce one chunk after 640 samples"
        # B's buffer still has its original 320; another B-frame should
        # complete its FIRST chunk now.
        probs_b2 = proc.process_frame_for_user(user_id=2, pcm_bytes=single_frame)
        assert len(probs_b2) == 1, (
            "B should produce its FIRST chunk now — proves buffer was "
            "isolated from A's 640-sample crossing"
        )

    def test_per_user_lstm_state_persists_across_frames(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When user A produces multiple chunks in a row, the second
        chunk's inference sees A's state from the first chunk. The
        restore-before/save-after dance must hand back A's exact state
        the next time A sends audio.
        """
        fake_model = _make_fake_model()
        monkeypatch.setattr(
            "poob.voice.silero_vad.load_silero_vad", lambda onnx=True: fake_model,
        )

        from poob.voice.silero_vad import SileroVADProcessor

        proc = SileroVADProcessor()
        # Push enough audio through A to trigger multiple chunks.
        single_frame = b"\x00" * 3840
        for _ in range(5):
            proc.process_frame_for_user(user_id=1, pcm_bytes=single_frame)

        # User B sends one frame between A's runs. This forces a state
        # save (A) + restore (B's blank) + save (B) flow.
        proc.process_frame_for_user(user_id=2, pcm_bytes=single_frame)

        # When A speaks again, the processor must have restored A's
        # saved state — not B's — before calling the model.
        a_state_saved = proc._per_user[1].saved_state
        a_context_saved = proc._per_user[1].saved_context
        assert a_state_saved is not None
        assert a_context_saved is not None

        # Snapshot what model._state should be when A's next frame fires
        proc.process_frame_for_user(user_id=1, pcm_bytes=single_frame)
        # The fake model's "set state" tracker should show A's state
        # was the LAST one restored (not B's).
        last_restored_marker = fake_model._last_restored_user
        assert last_restored_marker == 1, (
            f"Expected A (user_id=1) state to be restored last; got {last_restored_marker}"
        )


# ---------------------------------------------------------------------------
# VoiceSession integration — opt-in via config
# ---------------------------------------------------------------------------

class TestVoiceSessionIntegration:
    def test_silero_disabled_by_default(self) -> None:
        """``voice_use_silero_vad`` must default to False so the refactor
        doesn't change behavior on rollout. The energy-VAD path remains
        the default until the operator opts in via env var.
        """
        from poob.config import AppConfig

        config = AppConfig(
            _env_file=None,
            discord_bot_token="x",
            discord_deals_channel_id=1,
        )
        assert config.voice_use_silero_vad is False


# ---------------------------------------------------------------------------
# Fake-model helper — mocks the silero_vad.OnnxWrapper surface we use
# ---------------------------------------------------------------------------

def _make_fake_model() -> MagicMock:
    """Build a fake Silero model that mimics the ``_state`` / ``_context``
    tensor shapes the real ONNX wrapper exposes. Tracks which user's
    state was last restored via a ``_last_restored_user`` attribute so
    tests can assert restore-ordering.
    """
    fake = MagicMock()
    fake._last_restored_user = None  # set by the per-user-state restore path

    class _FakeTensor:
        """Mimics torch.Tensor.clone() for state snapshot tests."""

        def __init__(self, marker: int = 0) -> None:
            self._marker = marker

        def clone(self) -> "_FakeTensor":
            return _FakeTensor(marker=self._marker + 1)

    fake._state = _FakeTensor()
    fake._context = _FakeTensor()

    # Calling the model returns a tensor with .item() yielding a prob.
    def _call(*args, **kwargs):
        result = MagicMock()
        result.item = MagicMock(return_value=0.05)  # silence-ish probability
        return result

    fake.side_effect = _call
    fake.reset_states = MagicMock()
    return fake
