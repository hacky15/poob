"""Tests for the music-effect filter-preset module.

See docs/decisions/music-filter-presets.md for the design + validated
FFmpeg parameter sources.
"""

from __future__ import annotations

import pytest

from poob.music.effects import (
    AVAILABLE_EFFECTS,
    EFFECT_NONE,
    EFFECT_PRESETS,
    EffectNotFoundError,
    is_valid_effect,
    resolve_effect_chain,
)


# ---------------------------------------------------------------------------
# Registry shape — regression guards
# ---------------------------------------------------------------------------

def test_effect_none_is_a_sentinel_for_no_filter() -> None:
    """``EFFECT_NONE`` is the canonical 'remove all effects' value.
    Callers pass this to clear the active effect; respawn picks up
    a chain-free FFmpeg invocation."""
    assert EFFECT_NONE == "none"
    assert EFFECT_NONE in AVAILABLE_EFFECTS
    assert resolve_effect_chain(EFFECT_NONE) is None


def test_available_effects_matches_preset_dict_keys() -> None:
    """``AVAILABLE_EFFECTS`` is what the brain/UI offers; ``EFFECT_PRESETS``
    is what the dispatcher resolves. They must stay aligned — every
    advertised effect must have a recipe (or be EFFECT_NONE)."""
    advertised = set(AVAILABLE_EFFECTS) - {EFFECT_NONE}
    implemented = set(EFFECT_PRESETS.keys())
    assert advertised == implemented, (
        f"Advertised but not implemented: {advertised - implemented}; "
        f"Implemented but not advertised: {implemented - advertised}"
    )


def test_required_preset_names_are_present() -> None:
    """Top-of-roadmap effects must exist. Keep this list as a
    regression guard — removing a preset is a breaking UX change."""
    required = {
        "nightcore", "slowed", "slowed_reverb", "super_slowed",
        "bassboost", "8d", "vaporwave", "karaoke", "chipmunk", "deep",
    }
    missing = required - set(EFFECT_PRESETS.keys())
    assert not missing, f"Missing required effect presets: {missing}"


# ---------------------------------------------------------------------------
# Filter-chain content — sanity check the validated recipes
# ---------------------------------------------------------------------------

def test_nightcore_uses_asetrate_with_125_multiplier() -> None:
    """Roadmap-validated parameters. Don't drift without re-validating."""
    chain = resolve_effect_chain("nightcore")
    assert chain is not None
    assert "asetrate=44100*1.25" in chain
    assert "aresample=44100" in chain


def test_slowed_uses_asetrate_with_085_multiplier() -> None:
    chain = resolve_effect_chain("slowed")
    assert chain is not None
    assert "asetrate=44100*0.85" in chain


def test_slowed_reverb_includes_aecho_chain() -> None:
    chain = resolve_effect_chain("slowed_reverb")
    assert chain is not None
    assert "asetrate=44100*0.85" in chain
    assert "aecho=" in chain


def test_bassboost_includes_dynaudnorm_for_headroom() -> None:
    """``bass=g=8`` without ``dynaudnorm`` clips on consumer DACs.
    Roadmap specifies the pair as the validated combo."""
    chain = resolve_effect_chain("bassboost")
    assert chain is not None
    assert "bass=" in chain
    assert "dynaudnorm" in chain


def test_8d_uses_apulsator_at_0125_hz() -> None:
    chain = resolve_effect_chain("8d")
    assert chain is not None
    assert "apulsator=hz=0.125" in chain


def test_karaoke_uses_pan_to_subtract_center_channel() -> None:
    chain = resolve_effect_chain("karaoke")
    assert chain is not None
    assert "pan=" in chain
    assert "c0-c1" in chain  # the L=R subtraction


def test_super_slowed_uses_075_not_below_065() -> None:
    """Below 0.65 the audio becomes unlistenable per the research;
    0.75 is the documented floor for the user-facing 'super slowed' preset."""
    chain = resolve_effect_chain("super_slowed")
    assert chain is not None
    assert "asetrate=44100*0.75" in chain


def test_chains_have_no_unescaped_pipe_outside_aecho_or_pan() -> None:
    """FFmpeg pipe in arg-lists is fine as a literal; what matters is
    that we never accidentally emit a shell-pipe character. Our chains
    only use ``|`` inside aecho delays and pan channel maps."""
    for name, chain in EFFECT_PRESETS.items():
        if "|" in chain:
            assert any(
                tok in chain
                for tok in ("aecho=", "pan=stereo")
            ), f"Effect {name!r} uses '|' outside an expected filter context"


# ---------------------------------------------------------------------------
# Dispatch behavior
# ---------------------------------------------------------------------------

def test_resolve_unknown_effect_raises() -> None:
    with pytest.raises(EffectNotFoundError) as excinfo:
        resolve_effect_chain("notarealthing")
    assert "notarealthing" in str(excinfo.value)


def test_resolve_is_case_insensitive() -> None:
    """Voice STT can capitalize unpredictably ('Nightcore', 'NIGHTCORE')."""
    assert resolve_effect_chain("NIGHTCORE") == resolve_effect_chain("nightcore")
    assert resolve_effect_chain("SlOwEd") == resolve_effect_chain("slowed")


def test_is_valid_effect_accepts_known_names_only() -> None:
    assert is_valid_effect("nightcore")
    assert is_valid_effect("none")
    assert is_valid_effect("NIGHTCORE")  # case-insensitive
    assert not is_valid_effect("")
    assert not is_valid_effect("notarealthing")
    assert not is_valid_effect(None)  # type: ignore[arg-type]


def test_resolve_strips_whitespace() -> None:
    """STT may inject leading/trailing spaces from voice transcripts."""
    assert resolve_effect_chain("  nightcore  ") == resolve_effect_chain("nightcore")
