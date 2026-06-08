"""worth_attention parse behavior — fail-OPEN so weak cascade rungs that omit
the field never silently black out the public feed. The hard-False gate lives
on the public path in orchestrator._vlm_to_deal (tested in test_smart_deal_radar).
See docs/decisions/public-incredible-selectivity-floors.md.
"""

from __future__ import annotations

from poob.skills.models import VLMEvaluation
from poob.skills.vlm_evaluator import VLMDealEvaluator


def _parse(content: str) -> VLMEvaluation:
    # _parse_response reads no instance state; bypass __init__ (which needs providers).
    inst = object.__new__(VLMDealEvaluator)
    return VLMDealEvaluator._parse_response(inst, content)


def test_model_default_is_true():
    assert VLMEvaluation().worth_attention is True


def test_omitted_field_parses_true():
    out = _parse('{"deal_quality": "incredible", "estimated_value_mid": 100}')
    assert out.worth_attention is True


def test_explicit_false_parses_false():
    out = _parse('{"deal_quality": "pass", "worth_attention": false}')
    assert out.worth_attention is False


def test_explicit_true_parses_true():
    out = _parse('{"deal_quality": "good", "worth_attention": true}')
    assert out.worth_attention is True


def test_unparseable_response_defaults_true():
    out = _parse("not json at all")
    assert out.worth_attention is True
