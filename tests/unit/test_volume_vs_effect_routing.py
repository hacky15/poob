"""Volume and audio-effect are distinct commands in the routing prompt.

Regression for the 2026-06-09 bug: the effect-off fix had added bare 'normal'
as an apply_effect(none) trigger, which polluted the clean line — "normal
volume" got hijacked to the filter path (Gemini even hallucinated
super_slowed). The fix removes the 'normal' overload and states the principle:
volume = loudness, effect = a named filter. See
docs/decisions/now-playing-conversational-answers.md (effect routing) and the
incident note.
"""

from __future__ import annotations

from poob.brain.poob import _build_system_prompt


def test_bare_normal_is_no_longer_an_effect_clear_trigger() -> None:
    p = _build_system_prompt(5, voice=True)
    # The overloaded bare-'normal' trigger that hijacked "normal volume" is gone.
    assert "/ 'normal' " not in p
    assert "do NOT fire this just because the word" in p  # explicit guard


def test_volume_is_distinct_from_effect() -> None:
    low = _build_system_prompt(5, voice=True).lower()
    assert "volume is not an effect" in low
    assert "normal volume" in low
    assert "never apply_effect" in low  # loudness words must not route to a filter


def test_effect_clear_still_works_via_specific_phrasing() -> None:
    p = _build_system_prompt(5, voice=True)
    low = p.lower()
    # Effect removal still routes — just on effect language, not bare 'normal'.
    assert "clear effect" in low or "remove the effect" in low
    assert "effect='none'" in p


def test_effect_on_routing_unchanged() -> None:
    low = _build_system_prompt(5, voice=True).lower()
    assert "nightcore" in low and "slowed" in low  # the on-triggers survive
