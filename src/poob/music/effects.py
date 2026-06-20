"""Music effect presets — validated FFmpeg filter chains.

A single source of truth for the on-the-fly audio effects Poob can apply
to a playing track. The chains here are looked up by name and passed
verbatim to FFmpeg's ``-af`` flag when the player respawns its audio
source. See ``docs/decisions/music-filter-presets.md`` for the design
and ``docs/research/music-bot-feature-roadmap.md`` for the parameter
sources.

The recipes were validated against:

- FFmpeg's own filter docs (asetrate, aresample, atempo, aecho, apulsator,
  bass, asubboost, dynaudnorm, alimiter, acrusher, tremolo, vibrato) and
  re-confirmed by running each chain through ffmpeg on a sine input.
- The "slowed + reverb" TikTok-genre convention (asetrate 0.85x with a
  triple-tap aecho).
- The 8D-audio one-liner that became the community standard
  (apulsator hz=0.125 for one full pan per 8 seconds).

Adding a preset: pick a name, write the chain, drop it in
``EFFECT_PRESETS``, add the name to ``AVAILABLE_EFFECTS``. The lookup
helpers below normalize case and whitespace so STT-derived names like
``"  Nightcore  "`` resolve correctly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable


class EffectNotFoundError(ValueError):
    """Raised when a caller asks for an effect that's not in the registry."""


# Sentinel for "no filter, plain audio". Stored in the active-effect slot
# on GuildMusicPlayer; ``resolve_effect_chain`` returns ``None`` for it
# so callers can branch cleanly between "spawn with ``-af``" and "spawn
# without ``-af``" without doing string-emptiness checks.
EFFECT_NONE = "none"


# Validated FFmpeg chains. Values pass verbatim to ``-af``; do not add
# leading dashes or spaces. Pipes inside ``aecho`` and ``pan`` are
# literal characters FFmpeg expects — they're never shell-piped through
# a subprocess because GuildMusicPlayer passes args as a list, not a
# shell string.
EFFECT_PRESETS: dict[str, str] = {
    # Speed + pitch up via sample-rate trick. Most popular preset.
    "nightcore": "asetrate=44100*1.25,aresample=44100",
    # Speed + pitch down. 0.85 is the slowed-genre standard.
    "slowed": "asetrate=44100*0.85,aresample=44100",
    # Slowed paired with a triple-delay echo for the slowed+reverb sound.
    "slowed_reverb": ("asetrate=44100*0.85,aresample=44100,aecho=0.8:0.88:60|90|120:0.4|0.3|0.2"),
    # Floor of the listenable range; 0.65 and below becomes unintelligible.
    "super_slowed": "asetrate=44100*0.75,aresample=44100",
    # Bass boost with dynaudnorm to recover headroom; bass=g=8 without
    # the normalizer clips on consumer DACs.
    "bassboost": "bass=g=8,dynaudnorm=f=200",
    # 0.125 Hz is one full pan per 8 seconds — the "8D audio" effect.
    "8d": "apulsator=hz=0.125",
    # Slower than slowed (0.8x) plus a long-tail reverb for the vaporwave wash.
    "vaporwave": "asetrate=44100*0.8,aresample=44100,aecho=0.8:0.9:1000:0.3",
    # 1.5x asetrate is the chipmunk limit; above that loses intelligibility.
    "chipmunk": "asetrate=44100*1.5,aresample=44100",
    # Darth Vader: pitch DOWN ~6 semitones with tempo PRESERVED. asetrate
    # drops pitch+tempo to 0.72x, then atempo=1.389 (≈1/0.72) restores the
    # tempo so the beat stays normal while the voice goes deep; a short
    # metallic echo gives the helmet timbre. Supersedes the old "deep",
    # which was a literal duplicate of super_slowed (slowed everything, no
    # tempo restore) and never sounded like Vader.
    "darth_vader": ("asetrate=44100*0.72,aresample=44100,atempo=1.389,aecho=0.6:0.35:18:0.35"),
    # Aggressive low-shelf (+15 dB at 110 Hz) + sub-bass rumble, clamped by
    # a limiter so it slams without clipping consumer DACs. bassboost is the
    # gentle version (+8 dB); this is the wall-shaker / "overload" bass.
    "ultrabass": "bass=g=15:f=110,asubboost,alimiter=limit=0.9",
    # Bitcrushed overdrive — the chaos / "earrape" preset. acrusher at 6
    # bits with a limiter so it's nasty but not speaker-destroying.
    "overload": ("acrusher=level_in=1:level_out=1:bits=6:mode=log:aa=1,alimiter=limit=0.95"),
    # Hall reverb WITHOUT slowing — slowed_reverb couples reverb to a 0.85x
    # slowdown; this applies reverb to a full-speed track.
    "reverb": "aecho=0.8:0.9:500|750|1000:0.3|0.25|0.2",
    # Tremolo: amplitude (volume) wobble at 5 Hz.
    "tremolo": "tremolo=f=5:d=0.7",
    # Vibrato: pitch wobble at 6 Hz.
    "vibrato": "vibrato=f=6:d=0.5",
}


# What we advertise to the LLM tool schema and to UI buttons. Includes
# the EFFECT_NONE sentinel so users can clear an active effect via the
# same path.
AVAILABLE_EFFECTS: tuple[str, ...] = (EFFECT_NONE, *EFFECT_PRESETS.keys())


def _normalize(name: object) -> str:
    if not isinstance(name, str):
        return ""
    return name.strip().lower()


def resolve_effect_chain(name: str) -> str | None:
    """Return the FFmpeg ``-af`` chain for ``name``, or ``None`` for EFFECT_NONE.

    Raises ``EffectNotFoundError`` if the name isn't in the registry.
    Case + whitespace normalized so voice-STT names like ``"  Nightcore  "``
    resolve cleanly.
    """
    key = _normalize(name)
    if key == EFFECT_NONE:
        return None
    if key not in EFFECT_PRESETS:
        raise EffectNotFoundError(f"unknown effect {name!r}; valid: {', '.join(AVAILABLE_EFFECTS)}")
    return EFFECT_PRESETS[key]


def is_valid_effect(name: object) -> bool:
    """Cheap pre-validation for tool-args. ``None`` and empty -> False."""
    key = _normalize(name)
    if not key:
        return False
    return key == EFFECT_NONE or key in EFFECT_PRESETS


# --- Stacking support -------------------------------------------------------
# Effects that touch the SAME audio parameter can't sensibly co-exist (you
# can't be both nightcore AND slowed — both rewrite asetrate). So each preset
# has a CATEGORY; stacking keeps at most one effect per category (a new one in
# a category replaces the old), while effects in DIFFERENT categories layer.
# See docs/decisions/music-effect-stacking.md.
EFFECT_CATEGORIES: dict[str, str] = {
    # speed/pitch — all rewrite asetrate, mutually exclusive
    "nightcore": "speed",
    "slowed": "speed",
    "slowed_reverb": "speed",
    "super_slowed": "speed",
    "vaporwave": "speed",
    "chipmunk": "speed",
    "darth_vader": "speed",
    "bassboost": "bass",
    "ultrabass": "bass",
    "overload": "distortion",
    "reverb": "reverb",
    "tremolo": "modulation",
    "vibrato": "modulation",
    "8d": "pan",
}

# Order categories are applied in the combined FFmpeg chain — deterministic
# regardless of the order the user added them, and a sane signal path
# (speed/pitch → bass → distortion → reverb → modulation → spatial).
_CATEGORY_ORDER: tuple[str, ...] = (
    "speed",
    "bass",
    "distortion",
    "reverb",
    "modulation",
    "pan",
)


def effect_category(name: str) -> str:
    """Category of an effect (empty string if uncategorized)."""
    return EFFECT_CATEGORIES.get(_normalize(name), "")


# Natural-language phrasings → canonical preset name. Lets the router pass the
# user's words ("ultra slowed", "vader voice") instead of guessing the exact
# registry key. Normalized (lower + stripped) on lookup.
EFFECT_ALIASES: dict[str, str] = {
    "ultra slowed": "super_slowed",
    "ultraslowed": "super_slowed",
    "super slow": "super_slowed",
    "super slowed": "super_slowed",
    "extra slowed": "super_slowed",
    "really slowed": "super_slowed",
    "slow": "slowed",
    "slowed down": "slowed",
    "slow it down": "slowed",
    "sped up": "nightcore",
    "speed up": "nightcore",
    "fast": "nightcore",
    "high pitch": "chipmunk",
    "chipmunked": "chipmunk",
    "vader": "darth_vader",
    "vader voice": "darth_vader",
    "darth": "darth_vader",
    "darth vader": "darth_vader",
    "deep voice": "darth_vader",
    "max bass": "ultrabass",
    "ultra bass": "ultrabass",
    "heavy bass": "ultrabass",
    "bass overload": "ultrabass",
    "extra bass": "ultrabass",
    "bass boost": "bassboost",
    "boost the bass": "bassboost",
    "boost bass": "bassboost",
    "earrape": "overload",
    "distorted": "overload",
    "distortion": "overload",
    "overload it": "overload",
    "8d audio": "8d",
    "eight d": "8d",
    "surround": "8d",
    "vapor wave": "vaporwave",
    "off": EFFECT_NONE,
    "clear": EFFECT_NONE,
    "no effect": EFFECT_NONE,
    "none": EFFECT_NONE,
    "normal": EFFECT_NONE,
    "reset": EFFECT_NONE,
}


def resolve_effect_name(raw: object) -> str:
    """Resolve a user phrasing to a canonical preset name (or ``EFFECT_NONE``).

    Tries the alias table first, then the registry directly. Raises
    ``EffectNotFoundError`` if it resolves to nothing valid — so the caller
    gives the user a helpful "valid: ..." message instead of a silent miss.
    """
    key = _normalize(raw)
    if not key:
        raise EffectNotFoundError("no effect given")
    if key in EFFECT_ALIASES:
        return EFFECT_ALIASES[key]
    if key == EFFECT_NONE or key in EFFECT_PRESETS:
        return key
    raise EffectNotFoundError(f"unknown effect {raw!r}; valid: {', '.join(AVAILABLE_EFFECTS)}")


def _category_rank(category: str) -> int:
    """Sort key for the signal path; uncategorized sinks to the end."""
    return _CATEGORY_ORDER.index(category) if category in _CATEGORY_ORDER else len(_CATEGORY_ORDER)


# --- Parametric, adjustable effects ("more / less", "slower / faster") -------
# Most filters are a single scalar (a speed factor, a bass-gain dB, a reverb
# mix). Modelling them as a continuous LEVEL — instead of a fixed preset string
# — lets the user crank any of them up or down on the fly ("more reverb",
# "slower", "less bass") instead of toggling a frozen preset. Named presets
# become shortcuts that set starting levels. A couple of effects don't map to
# one knob (darth_vader's pitch-down-with-tempo-restore, overload's bitcrush)
# and stay ATOMIC — fixed chains, on/off only. See
# docs/decisions/music-effect-stacking.md.


def _speed_chain(factor: float) -> str | None:
    if abs(factor - 1.0) < 1e-3:
        return None
    return f"asetrate=44100*{factor:.4g},aresample=44100"


def _bass_chain(gain_db: float) -> str | None:
    g = round(gain_db, 1)
    if g <= 0:
        return None
    # Gentle boost normalizes headroom; heavy boost adds sub-rumble + a hard
    # limiter so it slams without clipping consumer DACs (the old ultrabass).
    if g >= 12:
        return f"bass=g={g:.3g}:f=110,asubboost,alimiter=limit=0.9"
    return f"bass=g={g:.3g},dynaudnorm=f=200"


def _reverb_chain(mix: float) -> str | None:
    if mix <= 0.05:
        return None
    # Triple-tap echo; tap decays scale with mix. mix=0.5 reproduces the
    # original `reverb` preset (0.3|0.25|0.2).
    d1, d2, d3 = 0.6 * mix, 0.5 * mix, 0.4 * mix
    return f"aecho=0.8:0.9:500|750|1000:{d1:.2g}|{d2:.2g}|{d3:.2g}"


def _eightd_chain(hz: float) -> str | None:
    return f"apulsator=hz={hz:.3g}"


def _tremolo_chain(depth: float) -> str | None:
    return f"tremolo=f=5:d={depth:.2g}"


def _vibrato_chain(depth: float) -> str | None:
    return f"vibrato=f=6:d={depth:.2g}"


@dataclass(frozen=True)
class EffectDimension:
    """One continuously-adjustable effect knob.

    ``inactive`` is the level at which the dimension produces no filter (1.0 for
    speed, 0 for additive effects). ``default`` is where "more <x>" turns it on
    from off. ``multiplicative`` steps multiply/divide by ``step``; otherwise
    they add/subtract it. ``builder`` renders the FFmpeg chain for a level.
    """

    category: str
    inactive: float
    default: float
    floor: float
    ceil: float
    multiplicative: bool
    step: float
    builder: Callable[[float], str | None]


DIMENSIONS: dict[str, EffectDimension] = {
    "speed": EffectDimension("speed", 1.0, 0.85, 0.5, 1.6, True, 1.22, _speed_chain),
    "bass": EffectDimension("bass", 0.0, 8.0, 2.0, 20.0, False, 4.0, _bass_chain),
    "reverb": EffectDimension("reverb", 0.0, 0.5, 0.15, 1.0, False, 0.2, _reverb_chain),
    "8d": EffectDimension("pan", 0.0, 0.125, 0.05, 0.6, True, 1.5, _eightd_chain),
    "tremolo": EffectDimension("modulation", 0.0, 0.7, 0.3, 0.95, False, 0.12, _tremolo_chain),
    "vibrato": EffectDimension("modulation", 0.0, 0.5, 0.2, 0.95, False, 0.12, _vibrato_chain),
}

# Named presets → the dimension levels they set. Applying a preset writes these
# levels (replacing whatever occupied those categories). Effects NOT here are
# atomic (see ATOMIC_PRESETS) or the EFFECT_NONE clear.
PRESET_DIMENSION_LEVELS: dict[str, dict[str, float]] = {
    "nightcore": {"speed": 1.25},
    "slowed": {"speed": 0.85},
    "super_slowed": {"speed": 0.75},
    "chipmunk": {"speed": 1.5},
    "vaporwave": {"speed": 0.8, "reverb": 0.8},
    "slowed_reverb": {"speed": 0.85, "reverb": 0.5},
    "bassboost": {"bass": 8.0},
    "ultrabass": {"bass": 16.0},
    "reverb": {"reverb": 0.5},
    "8d": {"8d": 0.125},
    "tremolo": {"tremolo": 0.7},
    "vibrato": {"vibrato": 0.5},
}

# Effects with no single knob — fixed chains from EFFECT_PRESETS, on/off only.
ATOMIC_PRESETS: frozenset[str] = frozenset({"darth_vader", "overload"})

# Effect/preset noun → the dimension a "more <x>" / "less <x>" should nudge.
EFFECT_TO_DIMENSION: dict[str, str] = {
    "speed": "speed",
    "slowed": "speed",
    "nightcore": "speed",
    "super_slowed": "speed",
    "chipmunk": "speed",
    "vaporwave": "speed",
    "bass": "bass",
    "bassboost": "bass",
    "ultrabass": "bass",
    "reverb": "reverb",
    "8d": "8d",
    "tremolo": "tremolo",
    "vibrato": "vibrato",
}

# Bare relative-speed words → step direction ("up" = faster, "down" = slower).
RELATIVE_SPEED: dict[str, str] = {
    "slower": "down",
    "faster": "up",
}


def dimension_of(effect: object) -> str | None:
    """The adjustable dimension a "more/less <effect>" targets, or ``None``."""
    key = _normalize(effect)
    if key in DIMENSIONS:
        return key
    return EFFECT_TO_DIMENSION.get(EFFECT_ALIASES.get(key, key))


def step_level(dim: str, current: float | None, direction: str) -> float | None:
    """New level after one "more"/"less" step on dimension ``dim``.

    ``current`` is the active level, or ``None`` if the dimension is off.
    ``direction`` is ``"up"`` (more/faster) or ``"down"`` (less/slower).
    Returns the clamped new level, or ``None`` if the step turns it OFF
    (additive effect stepped below its floor, or speed returned to ~1.0).
    """
    spec = DIMENSIONS[dim]
    if current is None:
        # "more X" from off turns it on at its audible default; "less X" from
        # off is a no-op. Speed is the exception: it has a non-zero neutral
        # (1.0) and is bidirectional — slower/faster both step from there.
        # (Using a 0-neutral dim's `inactive` as the base would multiply from
        # 0 and stick at the floor — the 8d "more from off" bug.)
        if spec.inactive:
            base = spec.inactive
        elif direction == "up":
            return _clamp_dim(spec, spec.default)
        else:
            return None  # 'less' from off = no-op
    else:
        base = current
    if spec.multiplicative:
        nxt = base * spec.step if direction == "up" else base / spec.step
    else:
        nxt = base + spec.step if direction == "up" else base - spec.step
    # Crossing the neutral (e.g. 'faster' from a slowed factor back through
    # 1.0) snaps to OFF instead of overshooting into the opposite regime.
    if current is not None and (base - spec.inactive) * (nxt - spec.inactive) < 0:
        return None
    # Zero-neutral dims fade OFF once a down-step reaches their floor. Additive
    # (bass/reverb) step below it; multiplicative (8d) only clamps to it, so
    # check the pre-clamp value here too — otherwise 8d sticks at the floor.
    if direction == "down" and spec.inactive == 0.0 and nxt <= spec.floor:
        return None
    nxt = _clamp_dim(spec, nxt)
    if abs(nxt - spec.inactive) < 1e-3:
        return None  # back to neutral = off
    return nxt


def _clamp_dim(spec: EffectDimension, level: float) -> float:
    return max(spec.floor, min(spec.ceil, level))


def render_effect_chain(levels: dict[str, float], atomic: Iterable[str]) -> str | None:
    """Combined FFmpeg ``-af`` for the active dimension levels + atomic presets.

    Ordered by category for a deterministic, sane signal path. Returns ``None``
    when nothing is active (caller spawns FFmpeg without ``-af``).
    """
    items: list[tuple[int, str]] = []
    for dim, level in levels.items():
        spec = DIMENSIONS.get(dim)
        if spec is None:
            continue
        chain = spec.builder(level)
        if chain:
            items.append((_category_rank(spec.category), chain))
    for name in atomic:
        chain = resolve_effect_chain(name)
        if chain:
            items.append((_category_rank(effect_category(name)), chain))
    items.sort(key=lambda item: item[0])
    rendered = [chain for _, chain in items]
    return ",".join(rendered) if rendered else None


def dimension_label(dim: str, level: float) -> str:
    """Short human label for an active dimension level (spoken summaries)."""
    if dim == "speed":
        for name, levels in PRESET_DIMENSION_LEVELS.items():
            if len(levels) == 1 and abs(levels.get("speed", -99.0) - level) < 1e-3:
                return name
        return f"{level:.2g}x speed"
    if dim == "bass":
        return "heavy bass" if level >= 12 else "bass boost"
    if dim == "reverb":
        return "heavy reverb" if level >= 0.7 else "reverb"
    return dim
