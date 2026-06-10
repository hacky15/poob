---
type: decision
status: active
date: 2026-05-12
tags: [music, ffmpeg, audio-effects, brain-tool]
related: [[music-on-the-fly-filter-respawn]] [[music-player-architecture]] [[toob-voice-filter-chain]]
---

# Music audio-effect presets — `apply_effect(effect: str)`

## Context

Music bots in 2025-2026 differentiate at the long tail: bots like Uzox, Jockie, and Chip ship named filter presets (nightcore / slowed / 8D / bassboost / etc.) and most users discover them via voice ("nightcore it" / "slow it down"). The roadmap [[music-bot-feature-roadmap]] validated the FFmpeg parameter values across published sources (FFmpeg docs, slowed-genre conventions, the 8D-audio TikTok community).

Toob/Boob TTS already uses module-level FFmpeg filter chains ([[toob-voice-filter-chain]]) — the substrate is proven. What was missing: a unified registry of presets, a dispatch helper, and a tool surface.

## Decision

Three-piece design:

### 1. ``poob.music.effects`` — single source of truth

Module-level ``EFFECT_PRESETS: dict[str, str]`` mapping preset name → FFmpeg ``-af`` chain. Validated chains from the roadmap:

| Preset | FFmpeg `-af` |
|---|---|
| ``nightcore`` | ``asetrate=44100*1.25,aresample=44100`` |
| ``slowed`` | ``asetrate=44100*0.85,aresample=44100`` |
| ``slowed_reverb`` | ``asetrate=44100*0.85,aresample=44100,aecho=0.8:0.88:60\|90\|120:0.4\|0.3\|0.2`` |
| ``super_slowed`` | ``asetrate=44100*0.75,aresample=44100`` |
| ``bassboost`` | ``bass=g=8,dynaudnorm=f=200`` |
| ``8d`` | ``apulsator=hz=0.125`` |
| ``vaporwave`` | ``asetrate=44100*0.8,aresample=44100,aecho=0.8:0.9:1000:0.3`` |
| ``chipmunk`` | ``asetrate=44100*1.5,aresample=44100`` |
| ``darth_vader`` | ``asetrate=44100*0.72,aresample=44100,atempo=1.389,aecho=0.6:0.35:18:0.35`` |
| ``ultrabass`` | ``bass=g=15:f=110,asubboost,alimiter=limit=0.9`` |
| ``overload`` | ``acrusher=level_in=1:level_out=1:bits=6:mode=log:aa=1,alimiter=limit=0.95`` |
| ``reverb`` | ``aecho=0.8:0.9:500\|750\|1000:0.3\|0.25\|0.2`` |
| ``tremolo`` | ``tremolo=f=5:d=0.7`` |
| ``vibrato`` | ``vibrato=f=6:d=0.5`` |

`karaoke` and `deep` were retired 2026-06-10 — see the Amendment below.

The pipe character inside ``aecho`` delays and ``pan`` channel maps is FFmpeg syntax, not shell — safe because ``GuildMusicPlayer._make_audio_source`` passes args as a list to ``discord.FFmpegPCMAudio`` (which uses ``subprocess`` with no shell expansion).

Public surface:

- ``EFFECT_NONE = "none"`` — sentinel for the no-filter path.
- ``AVAILABLE_EFFECTS: tuple[str, ...]`` — tuple of advertised names (including ``EFFECT_NONE``).
- ``resolve_effect_chain(name: str) -> str | None`` — case-insensitive, whitespace-tolerant lookup. ``None`` for ``EFFECT_NONE``, raises ``EffectNotFoundError`` otherwise.
- ``is_valid_effect(name: object) -> bool`` — cheap pre-validation for tool args.

### 2. ``GuildMusicPlayer.set_effect(effect)``

Stores the active effect (name + resolved chain) and requests a respawn at the current track position. See [[music-on-the-fly-filter-respawn]] for the respawn mechanism — same path used by ``replay()`` and ``previous()``.

If there's no current track, the effect is still stored so it applies to the next track played. This is the right UX for "set nightcore" before queuing songs.

Returns the applied effect name on success, ``None`` if no current track (effect stored but not applied immediately). Raises ``EffectNotFoundError`` for unknown effects — handler wraps that for the user.

### 3. ``music_assistant`` action ``apply_effect``

```
{ "action": "apply_effect", "effect": "<preset name>" }
```

Effect arg routes through ``set_effect``. Schema's ``effect`` field description includes hints for the LLM to map natural-language phrases:

- "nightcore it" → ``"nightcore"``
- "slow it down" → ``"slowed"``
- "add reverb" → ``"slowed_reverb"`` (user-expected; standalone reverb is rare)
- "turn off the effect" → ``"none"``

Handler response:

- Effect applied, has current track: ``[SILENT]Applied {effect}.``
- Effect applied, no current track: ``[SILENT]Effect '{effect}' set — applies on the next track.``
- ``"none"``: ``[SILENT]Audio effect cleared.``
- Unknown name: ``[SILENT]unknown effect 'wahwah'; valid: none, nightcore, …``
- Missing ``effect`` arg: ``[SILENT]Which effect? (none, nightcore, slowed, slowed_reverb, …)`` — surfaces the catalog when the LLM forgets to include the param.

## Alternatives considered

- **Hard-code chains inside ``set_effect``.** Mixes registry and dispatch; harder to extend. Module-level dict is cheap and obvious.
- **Expose each preset as its own action (``nightcore`` / ``slowed`` / ``8d`` / ...).** Bloats the enum. The current schema's ``apply_effect`` + ``effect`` arg is more general and lets us add presets without schema churn.
- **Use ``ffmpeg-python`` to build chains programmatically.** Overkill. The chains are static strings; a dict literal is the right shape.
- **Pitch-shift independent of speed via ``rubberband``.** Roadmap noted: ``rubberband`` is a build flag, not a default. Verify the FFmpeg in the production Docker image supports it before promising. Deferred — none of the current presets require it.

## Consequences

- Adding a preset is a one-line edit to ``EFFECT_PRESETS`` plus an entry on the required-presets regression-guard list in ``test_music_effects.py``. No schema changes; no LLM prompt changes (the existing ``effect`` description tells the model to use the named preset).
- Failure mode: if FFmpeg rejects the chain (build doesn't include a filter, syntax error), the player respawns with a broken subprocess and audio dies. Mitigations: chains are validated at module-import time via the test suite; the ``_make_audio_source`` log line includes ``effect=True`` so prod issues are diagnosable.
- The Toob/Boob TTS filter chain in ``voice/session.py`` is intentionally separate — that's a TTS-only persona substrate. Music effects live under ``music/effects.py``. Don't merge them.

## Validation

- 15 tests in ``tests/unit/test_music_effects.py`` cover: case/whitespace normalization, unknown-effect error, validated chain content (asetrate multipliers, aecho present in slowed_reverb, bass+dynaudnorm together, apulsator hz value, pan channel-subtract for karaoke), required-presets regression guard, AVAILABLE_EFFECTS ↔ EFFECT_PRESETS alignment.
- 4 tests in ``tests/unit/test_music_player_primitives.py`` cover ``set_effect`` state transitions.
- 4 tests in ``tests/unit/test_music_handler_actions.py`` cover handler dispatch.

## Rollback

Remove the ``apply_effect`` branch from the handler and the action from the enum. ``effects.py`` and the player's ``set_effect`` / ``_active_effect`` field can stay; they're inert without a tool to invoke them.

## Amendment — 2026-06-10: roster curation + `list_effects`

Operator review ("only the best effects, plus a real Darth Vader and ultrabass"):

- **Cut ``deep``** — it was a byte-identical duplicate of ``super_slowed`` and never sounded like Darth Vader (``asetrate`` alone drops pitch *and* tempo). **Cut ``karaoke``** — mid-quality center-channel cancellation (also guts bass/drums), low user value per [[music-bot-feature-roadmap]].
- **Added (operator requirements):** ``darth_vader`` — pitch down with tempo **preserved** via ``asetrate=0.72 → atempo=1.389`` (≈1/0.72) plus a short metallic echo for the helmet timbre; ``ultrabass`` — ``bass=g=15`` + ``asubboost`` clamped by ``alimiter`` so it slams without clipping (``bassboost`` stays as the gentle +8 dB version).
- **Added (operator picks):** ``overload`` (bitcrushed overdrive, limiter-clamped), ``reverb`` (hall reverb *without* the slowdown ``slowed_reverb`` couples in), ``tremolo``, ``vibrato``. (``telephone`` was offered and declined.)
- **New ``list_effects`` action** on ``music_assistant`` — answers "what effects do you have" by reciting ``AVAILABLE_EFFECTS`` from the registry (no hallucination). Returns a **non-``[SILENT]``** string so Poob *speaks* it (control confirmations stay silent). Routing hints for the new effects + the list query were added to ``_MUSIC_ROUTING_RULES``.
- **Validation:** every chain (kept + new) was re-run through ffmpeg on a sine input, and the **prod container's ffmpeg (7.1.4)** was confirmed to include ``atempo``, ``asubboost``, ``alimiter``, ``acrusher``, ``tremolo``, ``vibrato``. The ``rubberband`` pitch-shift alternative this doc deferred was avoided — the universal ``atempo`` trick sidesteps the build-flag risk.
