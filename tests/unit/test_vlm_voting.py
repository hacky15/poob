"""Tests for VLM cascade voting agreement logic."""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import MagicMock

import pytest

from agentic_scraper.llm.vlm_cascade import VLMCascade


@dataclass
class _FakeResponse:
    """Minimal response object for testing _check_agreement."""
    content: str


def _json_response(deal_quality: str, value_mid: float) -> _FakeResponse:
    """Build a fake VLM response with JSON content."""
    import json
    return _FakeResponse(content=json.dumps({
        "deal_quality": deal_quality,
        "estimated_value_mid": value_mid,
        "item_identified": "Test Item",
        "confidence": 0.8,
    }))


@pytest.fixture
def cascade() -> VLMCascade:
    """Create a VLMCascade with no providers (just testing _check_agreement)."""
    return VLMCascade(providers=[], voting_enabled=False)


class TestCheckAgreement:
    """Agreement detection: unanimous, majority, and disagreement."""

    def test_unanimous_full_agreement(self, cascade):
        """3 voters agree on quality AND values → 1.0."""
        responses = {
            "a": _json_response("good", 100.0),
            "b": _json_response("good", 105.0),
            "c": _json_response("good", 98.0),
        }
        assert cascade._check_agreement(responses) == 1.0

    def test_unanimous_quality_divergent_values(self, cascade):
        """3 voters agree on quality but NOT values → 0.7."""
        responses = {
            "a": _json_response("good", 50.0),
            "b": _json_response("good", 150.0),
            "c": _json_response("good", 200.0),
        }
        assert cascade._check_agreement(responses) == 0.7

    def test_majority_two_of_three(self, cascade):
        """2/3 voters agree on quality → 0.5 (majority)."""
        responses = {
            "a": _json_response("good", 100.0),
            "b": _json_response("good", 110.0),
            "c": _json_response("fair", 50.0),
        }
        assert cascade._check_agreement(responses) == 0.5

    def test_no_agreement_all_different(self, cascade):
        """3 different quality ratings → 0.0."""
        responses = {
            "a": _json_response("good", 100.0),
            "b": _json_response("fair", 50.0),
            "c": _json_response("incredible", 300.0),
        }
        assert cascade._check_agreement(responses) == 0.0

    def test_two_voters_agree(self, cascade):
        """2 voters with same quality → full/partial agreement."""
        responses = {
            "a": _json_response("great", 100.0),
            "b": _json_response("great", 105.0),
        }
        assert cascade._check_agreement(responses) == 1.0

    def test_two_voters_disagree(self, cascade):
        """2 voters with different quality → 0.0."""
        responses = {
            "a": _json_response("good", 100.0),
            "b": _json_response("fair", 50.0),
        }
        assert cascade._check_agreement(responses) == 0.0

    def test_single_voter_no_agreement(self, cascade):
        """Single response can't form agreement."""
        responses = {"a": _json_response("good", 100.0)}
        assert cascade._check_agreement(responses) == 0.0

    def test_majority_three_of_five(self, cascade):
        """3/5 voters agree → majority (0.5)."""
        responses = {
            "a": _json_response("good", 100.0),
            "b": _json_response("fair", 40.0),
            "c": _json_response("good", 110.0),
            "d": _json_response("great", 200.0),
            "e": _json_response("good", 95.0),
        }
        assert cascade._check_agreement(responses) == 0.5

    def test_even_split_no_majority(self, cascade):
        """2 good vs 2 fair (even split) → no majority → 0.0."""
        responses = {
            "a": _json_response("good", 100.0),
            "b": _json_response("fair", 50.0),
            "c": _json_response("good", 110.0),
            "d": _json_response("fair", 55.0),
        }
        assert cascade._check_agreement(responses) == 0.0

    def test_invalid_json_ignored(self, cascade):
        """Non-JSON responses are ignored, remaining are evaluated."""
        responses = {
            "a": _json_response("good", 100.0),
            "b": _FakeResponse(content="this is not json"),
            "c": _json_response("good", 110.0),
        }
        assert cascade._check_agreement(responses) == 1.0


class TestPickMajorityResponse:
    """Response selection from the majority group."""

    def test_unanimous_picks_first(self, cascade):
        """Unanimous → first by priority order."""
        responses = {
            "groq_vision": _json_response("good", 100.0),
            "gemini_flash": _json_response("good", 105.0),
        }
        assert cascade._pick_majority_response(responses, 1.0) == "groq_vision"

    def test_majority_picks_from_majority_group(self, cascade):
        """Majority → first provider that voted with majority."""
        responses = {
            "groq_vision": _json_response("fair", 50.0),
            "gemini_flash": _json_response("good", 100.0),
            "gemma_vlm": _json_response("good", 110.0),
        }
        # "good" is the majority — gemini_flash is the first in that group
        assert cascade._pick_majority_response(responses, 0.5) == "gemini_flash"

    def test_majority_picks_first_by_priority(self, cascade):
        """When first provider is in majority, it's selected."""
        responses = {
            "groq_vision": _json_response("good", 100.0),
            "gemini_flash": _json_response("fair", 50.0),
            "gemma_vlm": _json_response("good", 110.0),
        }
        assert cascade._pick_majority_response(responses, 0.5) == "groq_vision"
