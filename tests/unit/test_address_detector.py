"""Tests for MultiSignalAddressDetector model-load behavior.

Focus: the Model2Vec semantic-scoring load is lazy, idempotent, and
self-limiting. A failed import must latch (not retry + re-warn on every
addressee decision) — the production bug where "Model2Vec unavailable"
spammed the logs because the failure wasn't cached. See
docs/incidents/text-mode-rlhf-refusal-leak-2026-05-29.md.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from poob.voice.address_detector import MultiSignalAddressDetector


def test_ensure_model_latches_on_failure() -> None:
    det = MultiSignalAddressDetector()
    assert det._embed_model is None
    assert det._embed_load_failed is False

    # First call: import succeeds but from_pretrained raises.
    with patch("model2vec.StaticModel.from_pretrained", side_effect=RuntimeError("no model")) as fp:
        det._ensure_model()
        assert det._embed_load_failed is True
        assert det._embed_model is None
        assert fp.call_count == 1

        # Second call must NOT retry (latch) — no second from_pretrained call.
        det._ensure_model()
        assert fp.call_count == 1


def test_ensure_model_loads_once_on_success() -> None:
    det = MultiSignalAddressDetector()
    fake_model = MagicMock()

    with patch("model2vec.StaticModel.from_pretrained", return_value=fake_model) as fp:
        det._ensure_model()
        assert det._embed_model is fake_model
        assert det._embed_load_failed is False
        assert fp.call_count == 1

        # Idempotent: already loaded → no second load.
        det._ensure_model()
        assert fp.call_count == 1


def test_semantic_relevance_safe_when_model_disabled() -> None:
    """With no embed model, semantic relevance returns 0.0 (neutral),
    never raises — the detector falls back to its other signals."""
    det = MultiSignalAddressDetector()
    det._embed_load_failed = True  # force disabled
    det._ensure_model()
    assert det._compute_semantic_relevance("anything at all") == 0.0
