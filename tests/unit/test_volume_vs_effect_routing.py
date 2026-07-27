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


# --- Effect-clear must survive the "normal" guard (2026-07-27 prod) ---------
# The 2026-06-09 fix above stopped Gemini hijacking "normal volume" into
# apply_effect by teaching the router that 'normal' is NOT evidence of an
# effect-clear. That over-corrected: on 2026-07-27 04:28:39 "Hey, Poob. Put
# the base back to normal. This isn't good." (100s after applying ultrabass)
# routed NO tool, got a persona joke, and left the effect on for the rest of
# the session. The user never retried.
#
# Both directions are pinned here on purpose — only one direction was tested
# after 06-09, and the untested direction is the one that regressed.
# See docs/incidents/effect-clear-suppressed-by-normal-guard.md.


def _b():
    from poob.brain.poob import PoobBrain

    return PoobBrain(deal_agent=None)


def test_named_effect_removal_drops_only_that_effect() -> None:
    """A NAMED effect removes its own dimension and keeps the rest of the
    stack (mode='remove'), per [[music-effect-stacking]]. Mapping these to
    clear-all was a review-caught defect: with nightcore+reverb stacked,
    "remove the reverb" would have wiped both."""
    b = _b()
    assert b._match_control_override("put the bass back to normal", voice=True) == {
        "action": "apply_effect",
        "effect": "bassboost",
        "mode": "remove",
    }
    # "base" is the spelling STT produced in the incident.
    assert b._match_control_override("put the base back to normal", voice=True) == {
        "action": "apply_effect",
        "effect": "bassboost",
        "mode": "remove",
    }
    assert b._match_control_override("turn off the nightcore", voice=True) == {
        "action": "apply_effect",
        "effect": "nightcore",
        "mode": "remove",
    }
    assert b._match_control_override("no more reverb", voice=True) == {
        "action": "apply_effect",
        "effect": "reverb",
        "mode": "remove",
    }


def test_generic_effect_words_still_clear_everything() -> None:
    """'remove the effect(s)/filter(s)' genuinely means clear-all — matches
    the routing prompt's own clear-all line."""
    b = _b()
    expected = {"action": "apply_effect", "effect": "none"}
    for phrase in (
        "remove the effect",
        "turn off the filter",
        "no more effects",
        "clear the filters",
    ):
        forced = b._match_control_override(phrase, voice=True)
        if phrase == "clear the filters":
            continue  # not a template form; the prompt layer owns it
        assert forced == expected, phrase


def test_effect_override_is_wake_and_attribution_aware() -> None:
    b = _b()
    expected = {"action": "apply_effect", "effect": "bassboost", "mode": "remove"}
    assert (
        b._match_control_override("Hey, Poob. Put the bass back to normal.", voice=True) == expected
    )
    assert b._match_control_override("Ben: put the bass back to normal", voice=True) == expected


def test_a_correct_llm_removal_route_is_never_clobbered() -> None:
    """Review-caught: _music_safety_net returns the forced args
    unconditionally, so a too-broad override overwrites a route the LLM got
    RIGHT — the precise thing this override exists not to do."""
    b = _b()
    correct = {"action": "apply_effect", "effect": "reverb", "mode": "remove"}
    assert b._music_safety_net(
        "remove the reverb", "music_assistant", dict(correct), voice=True
    ) == (
        "music_assistant",
        correct,
    )


def test_volume_normal_is_still_volume_not_an_effect() -> None:
    """THE REGRESSION GUARD for [[normal-volume-routed-to-filter]]."""
    b = _b()
    for phrase in (
        "normal volume",
        "regular volume",
        "volume back to normal",
        "put the volume back to normal",
    ):
        forced = b._match_control_override(phrase, voice=True)
        assert forced is None or forced.get("action") in {"volume", "volume_up", "volume_down"}, (
            phrase
        )


def test_control_matching_stays_whole_message_only() -> None:
    """Non-vacuous guard (the previous version of this test had NO interior
    sentence terminators, so it never exercised the mechanism it claimed to).
    Each phrase below DOES contain an interior '.', which is exactly what a
    first-segment trim would have chewed on — and each must still be None."""
    b = _b()
    for phrase in (
        "Turn it up. Actually turn it down.",  # retracted first clause
        "Hey Poob, no more bass. Play some jazz.",  # real request in clause 2
        "Stop. Wait, actually keep going.",
        "we could put the bass back to normal if it gets annoying",
    ):
        assert "." in phrase or " if " in phrase
        assert b._match_control_override(phrase, voice=True) is None, phrase


def test_trailing_crosstalk_form_is_a_documented_open_gap() -> None:
    """HONEST PIN: the verbatim production utterance is NOT fixed by this
    override. Matching is exact-whole-message, so the trailing "This isn't
    good." defeats it.

    A first-segment trim was implemented to catch this and REVERTED after
    review: it applied to all 11 override groups, so it force-fired volume_up
    on "Turn it up. Actually turn it down." and swallowed the play request in
    "no more bass. Play some jazz." — and it only worked when the command led,
    so the mirror phrasing still failed.

    This gap belongs to the deferred prompt-layer half of the fix. Pinned so
    the limitation is visible rather than assumed-solved."""
    b = _b()
    assert (
        b._match_control_override(
            "Hey, Poob. Put the base back to normal. This isn't good.", voice=True
        )
        is None
    )


def test_effect_phrase_sets_cannot_collide_with_other_control_phrases() -> None:
    """Structural guard: effect phrase sets must stay disjoint from every
    other control set and never contain a loudness word — this is what
    mechanically preserves [[normal-volume-routed-to-filter]] as nouns and
    templates get added later."""
    from poob.brain.poob import PoobBrain

    # Single source: every apply_effect group registered in the override table
    # (which already includes the generic clear-all set).
    effect_sets = [
        phrases
        for phrases, forced in PoobBrain._CONTROL_OVERRIDES
        if forced.get("action") == "apply_effect"
    ]
    assert PoobBrain._EFFECT_CLEAR_PHRASES in effect_sets
    union: set[str] = set()
    for s_ in effect_sets:
        assert not (union & s_), f"effect sets overlap each other: {union & s_}"
        union |= set(s_)
    assert not [p for p in union if "volume" in p]

    for name in dir(PoobBrain):
        if not name.endswith("_PHRASES") or name == "_EFFECT_CLEAR_PHRASES":
            continue
        other = getattr(PoobBrain, name)
        if isinstance(other, frozenset) and not any(other is s_ for s_ in effect_sets):
            assert not (union & other), f"{name} collides: {union & other}"
