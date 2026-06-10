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
    "slowed_reverb": (
        "asetrate=44100*0.85,aresample=44100,"
        "aecho=0.8:0.88:60|90|120:0.4|0.3|0.2"
    ),
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
    "darth_vader": (
        "asetrate=44100*0.72,aresample=44100,atempo=1.389,"
        "aecho=0.6:0.35:18:0.35"
    ),
    # Aggressive low-shelf (+15 dB at 110 Hz) + sub-bass rumble, clamped by
    # a limiter so it slams without clipping consumer DACs. bassboost is the
    # gentle version (+8 dB); this is the wall-shaker / "overload" bass.
    "ultrabass": "bass=g=15:f=110,asubboost,alimiter=limit=0.9",
    # Bitcrushed overdrive — the chaos / "earrape" preset. acrusher at 6
    # bits with a limiter so it's nasty but not speaker-destroying.
    "overload": (
        "acrusher=level_in=1:level_out=1:bits=6:mode=log:aa=1,"
        "alimiter=limit=0.95"
    ),
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
        raise EffectNotFoundError(
            f"unknown effect {name!r}; valid: {', '.join(AVAILABLE_EFFECTS)}"
        )
    return EFFECT_PRESETS[key]


def is_valid_effect(name: object) -> bool:
    """Cheap pre-validation for tool-args. ``None`` and empty -> False."""
    key = _normalize(name)
    if not key:
        return False
    return key == EFFECT_NONE or key in EFFECT_PRESETS
