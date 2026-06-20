"""Tests for the parametric effects engine.

Operator (2026-06-20): effects must apply on the fly and be incrementally
adjustable — "slower and slower", "more/less reverb", "more/less bass" — not
just toggled presets. Each adjustable filter is a continuous LEVEL that
"more"/"less" steps; presets set starting levels; darth_vader/overload are
atomic. See docs/decisions/music-effect-stacking.md.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from poob.music import effects as fx
from poob.music.player import GuildMusicPlayer


# --- aliases / resolve_effect_name -----------------------------------------


@pytest.mark.parametrize(
    "raw,canon",
    [
        ("ultra slowed", "super_slowed"),
        ("vader voice", "darth_vader"),
        ("max bass", "ultrabass"),
        ("8d audio", "8d"),
        ("  Nightcore ", "nightcore"),
        ("off", "none"),
    ],
)
def test_resolve_effect_name(raw: str, canon: str) -> None:
    assert fx.resolve_effect_name(raw) == canon


def test_resolve_unknown_raises() -> None:
    with pytest.raises(fx.EffectNotFoundError):
        fx.resolve_effect_name("wubwubwub")


# --- dimension_of (which knob does "more/less X" turn) ----------------------


@pytest.mark.parametrize(
    "noun,dim",
    [
        ("bass", "bass"),
        ("bassboost", "bass"),
        ("ultrabass", "bass"),
        ("max bass", "bass"),  # alias resolves first
        ("reverb", "reverb"),
        ("slowed", "speed"),
        ("nightcore", "speed"),
        ("8d", "8d"),
    ],
)
def test_dimension_of(noun: str, dim: str) -> None:
    assert fx.dimension_of(noun) == dim


def test_dimension_of_atomic_is_none() -> None:
    assert fx.dimension_of("darth_vader") is None  # not adjustable


# --- step_level (the more/less / slower-faster math) ------------------------


def test_step_speed_slower_and_slower_keeps_going() -> None:
    """The headline: 'slower' three times keeps dropping the factor."""
    f1 = fx.step_level("speed", None, "down")
    assert f1 is not None
    f2 = fx.step_level("speed", f1, "down")
    assert f2 is not None
    f3 = fx.step_level("speed", f2, "down")
    assert f3 is not None
    assert f1 > f2 > f3


def test_step_speed_clamps_at_floor() -> None:
    floor = fx.DIMENSIONS["speed"].floor
    factor: float | None = 1.0
    last = 1.0
    for _ in range(30):
        nxt = fx.step_level("speed", factor, "down")
        if nxt is None:
            break
        last = nxt
        factor = nxt
    assert last >= floor - 1e-9
    assert abs(last - floor) < 0.06  # actually reaches the floor


def test_step_speed_back_to_normal_turns_off() -> None:
    down = fx.step_level("speed", None, "down")
    assert fx.step_level("speed", down, "up") is None  # neutral = off


def test_step_reverb_more_from_off_turns_on_at_default() -> None:
    assert fx.step_level("reverb", None, "up") == fx.DIMENSIONS["reverb"].default


def test_step_reverb_less_eventually_fades_off() -> None:
    level: float | None = fx.DIMENSIONS["reverb"].default
    for _ in range(20):
        level = fx.step_level("reverb", level, "down")
        if level is None:
            break
    assert level is None


def test_step_less_from_off_is_noop() -> None:
    assert fx.step_level("reverb", None, "down") is None


def test_step_bass_more_caps_at_ceiling() -> None:
    level = fx.DIMENSIONS["bass"].ceil
    capped = fx.step_level("bass", level, "up")
    assert capped == fx.DIMENSIONS["bass"].ceil


def test_step_8d_more_from_off_lands_on_default_not_floor() -> None:
    # 8d is the one multiplicative, zero-neutral dimension; "more from off"
    # must turn it on at the audible default, not multiply 0 → floor.
    lv = fx.step_level("8d", None, "up")
    assert lv == fx.DIMENSIONS["8d"].default
    assert lv != fx.DIMENSIONS["8d"].floor


def test_step_8d_less_eventually_fades_off() -> None:
    # 8d (multiplicative, zero-neutral) must fade OFF on "less", not stick at
    # the floor forever — same contract as additive reverb/bass.
    level: float | None = fx.DIMENSIONS["8d"].default
    for _ in range(20):
        level = fx.step_level("8d", level, "down")
        if level is None:
            break
    assert level is None


def test_step_faster_from_slowed_returns_to_off_not_speedup() -> None:
    # 'faster' from a slowed factor returns toward normal and turns OFF when it
    # crosses 1.0, instead of overshooting into nightcore territory.
    assert fx.step_level("speed", 0.85, "up") is None  # 0.85→1.037 crosses → off
    # From a deeper slow it steps gradually toward normal (no overshoot), then
    # turns off on the step that crosses 1.0 — never jumps past into sped-up.
    once = fx.step_level("speed", 0.75, "up")
    assert once is not None and once < 1.0  # 0.75→0.915, still slowed
    assert fx.step_level("speed", once, "up") is None  # next step crosses → off
    # symmetric: 'slower' from a sped-up factor returns to off too
    assert fx.step_level("speed", 1.25, "down") is not None  # 1.25→1.025, still up
    assert fx.step_level("speed", 1.05, "down") is None  # 1.05→0.86 crosses → off


# --- render_effect_chain ----------------------------------------------------


def test_render_single_speed() -> None:
    assert fx.render_effect_chain({"speed": 0.85}, []) == "asetrate=44100*0.85,aresample=44100"


def test_render_stacks_in_category_order() -> None:
    chain = fx.render_effect_chain({"reverb": 0.7, "speed": 0.85, "bass": 16.0}, [])
    assert chain is not None
    assert chain.index("asetrate") < chain.index("bass=") < chain.index("aecho")


def test_render_includes_atomic_preset() -> None:
    chain = fx.render_effect_chain({"bass": 8.0}, ["darth_vader"])
    assert chain is not None
    assert "atempo" in chain  # darth_vader's tempo restore
    assert "bass=" in chain


def test_render_empty_is_none() -> None:
    assert fx.render_effect_chain({}, []) is None


def test_render_bass_heavy_adds_subboost() -> None:
    light = fx.render_effect_chain({"bass": 8.0}, [])
    heavy = fx.render_effect_chain({"bass": 16.0}, [])
    assert light is not None and "asubboost" not in light
    assert heavy is not None and "asubboost" in heavy


# --- dimension_label --------------------------------------------------------


def test_speed_labels() -> None:
    assert fx.dimension_label("speed", 1.25) == "nightcore"
    assert fx.dimension_label("speed", 0.85) == "slowed"
    assert fx.dimension_label("speed", 0.6).endswith("x speed")


def test_bass_reverb_labels() -> None:
    assert fx.dimension_label("bass", 16.0) == "heavy bass"
    assert fx.dimension_label("bass", 8.0) == "bass boost"
    assert fx.dimension_label("reverb", 0.8) == "heavy reverb"


# --- player-level (set / add / remove / adjust) -----------------------------


def _player() -> GuildMusicPlayer:
    p = GuildMusicPlayer.__new__(GuildMusicPlayer)
    p._effect_levels = {}
    p._atomic_effects = []
    p._active_effect_chain = None
    q = MagicMock()
    q.current = None  # no track → _apply_effect_chain renders then returns None
    p.queue = q
    return p


@pytest.mark.asyncio
async def test_add_stacks_cross_category() -> None:
    p = _player()
    await p.add_effect("slowed")
    await p.add_effect("ultrabass")
    await p.add_effect("reverb")
    assert p._effect_levels == {"speed": 0.85, "bass": 16.0, "reverb": 0.5}
    assert "asetrate" in p._active_effect_chain
    assert "bass=" in p._active_effect_chain
    assert "aecho" in p._active_effect_chain


@pytest.mark.asyncio
async def test_add_same_category_replaces() -> None:
    p = _player()
    await p.add_effect("slowed")
    await p.add_effect("nightcore")
    assert p._effect_levels == {"speed": 1.25}


@pytest.mark.asyncio
async def test_set_replaces_everything() -> None:
    p = _player()
    await p.add_effect("slowed")
    await p.add_effect("ultrabass")
    await p.set_effect("nightcore")
    assert p._effect_levels == {"speed": 1.25}


@pytest.mark.asyncio
async def test_adjust_slower_and_slower() -> None:
    p = _player()
    await p.adjust_effect("slower")
    f1 = p._effect_levels["speed"]
    await p.adjust_effect("slower")
    f2 = p._effect_levels["speed"]
    await p.adjust_effect("slower")
    f3 = p._effect_levels["speed"]
    assert f1 > f2 > f3


@pytest.mark.asyncio
async def test_adjust_more_reverb_compounds() -> None:
    p = _player()
    await p.adjust_effect("reverb", "up")
    assert p._effect_levels["reverb"] == fx.DIMENSIONS["reverb"].default
    await p.adjust_effect("reverb", "up")
    assert p._effect_levels["reverb"] > fx.DIMENSIONS["reverb"].default


@pytest.mark.asyncio
async def test_adjust_less_bass_fades_off() -> None:
    p = _player()
    await p.add_effect("bassboost")  # bass 8
    for _ in range(10):
        await p.adjust_effect("bass", "down")
        if "bass" not in p._effect_levels:
            break
    assert "bass" not in p._effect_levels


@pytest.mark.asyncio
async def test_adjust_takes_over_speed_from_preset() -> None:
    p = _player()
    await p.add_effect("nightcore")  # speed 1.25
    await p.adjust_effect("slower")
    assert p._effect_levels["speed"] < 1.25


@pytest.mark.asyncio
async def test_adjust_slower_clears_atomic_speed_preset() -> None:
    p = _player()
    await p.add_effect("darth_vader")  # atomic, speed category
    await p.adjust_effect("slower")
    assert "darth_vader" not in p._atomic_effects
    assert "speed" in p._effect_levels


@pytest.mark.asyncio
async def test_remove_one_leaves_others() -> None:
    p = _player()
    await p.add_effect("slowed")
    await p.add_effect("ultrabass")
    await p.remove_effect("bass")
    assert "bass" not in p._effect_levels
    assert p._effect_levels.get("speed") == 0.85


@pytest.mark.asyncio
async def test_none_clears_all() -> None:
    p = _player()
    await p.add_effect("slowed")
    await p.add_effect("reverb")
    await p.set_effect("none")
    assert p._effect_levels == {}
    assert p._active_effect_chain is None


@pytest.mark.asyncio
async def test_adjust_unknown_raises() -> None:
    p = _player()
    with pytest.raises(fx.EffectNotFoundError):
        await p.adjust_effect("darth_vader", "up")  # atomic, not adjustable


def test_active_effect_summary() -> None:
    p = _player()
    assert p.active_effect == "none"
    p._effect_levels = {"speed": 0.85, "bass": 16.0}
    summary = p.active_effect
    assert "slowed" in summary
    assert "heavy bass" in summary
