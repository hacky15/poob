---
type: decision
status: active
date: 2026-06-19
tags: [voice, music, effects, ffmpeg, toob]
related: [[toob-voice-filter-chain]] [[music-player-architecture]] [[voice-architecture]] [[slim-tool-schemas]] [[music-filter-presets]] [[music-on-the-fly-filter-respawn]]
---

# Music effects: parametric levels, stack, adjust on the fly, speak the list

## Context

Operator reports against the original single-effect, fixed-preset model (2026-06-19/20):

1. **"It's not stacking the filters."** "Keep all existing filters but add bass boost" *replaced* the active effect. `set_effect` held one `_active_effect` string; every apply overwrote the previous.
2. **"I asked what filters it had and it read snark, not the list."** `list_effects` computed the list but in **voice** mode `_handle_music` routes short (<300 char) non-`[SILENT]` responses through `_wrap_in_personality`, which *regenerates* a persona line and discards the content.
3. **"The effect names don't work" ("ultra slowed" → unknown).** `resolve_effect_chain` only matched exact registry keys.
4. **"Make it slower and slower."** The slow presets were `slowed` (0.85) → `super_slowed` (0.75) and nothing below — same category, so they *swapped*, not compounded. A third "slower" did nothing.
5. **"On-demand for almost all filters — speed up more/more, reverb more/less, customizable."** Fixed preset strings can't be nudged; the user wants to crank any adjustable filter up or down live.

## Decision

### 1. Parametric levels (the core model)

Each adjustable filter is a continuous **level**, not a frozen preset string. `effects.DIMENSIONS` defines the knobs — `speed` (asetrate factor), `bass` (gain dB), `reverb` (echo mix), `8d` (pan Hz), `tremolo`/`vibrato` (depth) — each with a category, floor/ceil, default, step, and a chain **builder**. `GuildMusicPlayer` holds `_effect_levels: dict[dim → level]` plus `_atomic_effects` (presets with no single knob: `darth_vader`, `overload`). `render_effect_chain` builds the combined, category-ordered `-af` from the active levels + atomic chains.

Named presets become **shortcuts that set levels** (`PRESET_DIMENSION_LEVELS`): nightcore→`speed 1.25`, slowed→`speed 0.85`, ultrabass→`bass 16`, reverb→`reverb 0.5`, slowed_reverb→`speed 0.85 + reverb 0.5`, etc. So "make it nightcore" still works; it just writes a level the user can then nudge.

- **At most one occupant per category** (a dimension *or* an atomic preset). A second speed effect swaps; speed + bass + reverb layer. Bass at ≥12 dB auto-adds the sub-rumble + limiter (the old `ultrabass` character) — a smooth crank from gentle to wall-shaker.
- **Deterministic order** (speed → bass → distortion → reverb → modulation → pan) regardless of apply order.

### 2. Adjust on the fly — "more/less", "slower/faster"

`player.adjust_effect(target, direction)` steps a dimension via `effects.step_level`:

- **"slower" / "faster"** (bare words, carry their own direction) step the `speed` factor multiplicatively (×0.82 / ÷0.82), clamped to **[0.5, 1.6]** — so "slower… slower… slower" keeps going down to half-speed instead of capping. Returning to ~1.0 turns speed off.
- **"more <x>" / "less <x>"** step any adjustable dimension up/down (`more reverb`, `less bass`, `more 8d`). "more" from off turns it on at its audible default; "less" below the floor fades it off.
- **Intent-aware direction** (`effects.adjust_direction`): "more" is up for most effects, but speed presets carry meaning — "more slowed" = *slower* (down), "more nightcore" = *faster* (up). Without this, "more slowed" stepped speed UP, crossed 1.0, and removed the slowdown (prod bug 2026-06-21).
- Adjusting takes over the category — "slower" while `darth_vader` (atomic, speed) is on clears Vader and starts a clean speed factor.

All of this respawns FFmpeg at the **current position** (the existing on-the-fly mechanism — [[music-on-the-fly-filter-respawn]]), so it applies mid-song with the ~200-400 ms gap and persists onto the next track.

### 3. `apply_effect` modes

`mode` ∈ `add` (default — stack/layer), `replace` ("only X"), `remove` (drop one), `more`/`less` (adjust). `none`/`clear` wipes everything. Default `add` because the operator's mental model is layering.

### 4. Aliases

`effects.resolve_effect_name` (presets/phrasings) and `effects.dimension_of` ("more <noun>" → dimension) map natural words → canonical knobs, so the router passes the user's phrasing.

### 5. `[SPEAK]` verbatim protocol

Sibling of `[SILENT]`. A handler returning `[SPEAK]<text>` is returned **verbatim** by `_handle_music` — never persona-wrapped — so `list_effects` actually reads the list (fixes report #2 at the source). The Toob/Boob voice sentinel still applies, so it's in-character with real content.

## Alternatives considered

- **Discrete "slower" ladder (add more fixed presets).** Rejected — still caps at the lowest preset and is magic-number soup; a continuous factor is the real "slower and slower".
- **Two asetrate filters (preset + a relative trim).** Rejected — chaining two `asetrate` double-resamples and compounds confusingly. One canonical speed factor owns the asetrate.
- **Free-form stacking, no categories.** Rejected — `nightcore` + `slowed` both write asetrate → nonsense. Category exclusivity keeps every combination valid.
- **Pad `list_effects` >300 chars to hit the verbatim bypass.** Rejected as a bandaid — `[SPEAK]` is the explicit contract.

## Consequences

- `player.active_effect` is a human summary ("slowed, heavy bass, reverb"); `active_effects` is the label list; `effect_levels` exposes the raw levels. `active_effect == "nightcore"` still holds for a single speed preset.
- The fixed `EFFECT_PRESETS` strings remain the source for **atomic** presets + the schema/alias surface; parametric dimensions render from builders, so `combine_chains` was removed.
- The tool `mode` enum grew (`+more/+less`); schema stays within the budget guarded by `test_tool_schema_budget`. Routing rules gained the adjust guidance.
- One FFmpeg respawn per change (~200-400 ms gap — [[toob-voice-filter-chain]]); adjusting doesn't add gaps.

## Validation

`tests/unit/test_effect_stacking.py` (42 — aliases, `dimension_of`, `step_level` slower-and-slower/clamps/fade-off, `render_effect_chain` order + heavy-bass subboost, labels, player set/add/remove/adjust) + `test_music_handler_actions` (add/replace/remove/more/less/slower dispatch) + `test_music_player_primitives` (on-the-fly respawn-at-position for set + adjust) + `test_slim_routing_prompt`. Full effect/music/schema set: 184 passing.

## Not done here (separate concern)

The VC router's **Gemini overflow rung** uses `gemini_router_model_alt = gemini-2.5-flash-lite` (a second free-tier RPM bucket behind primary `gemini-3.1-flash-lite-preview`). Seeing 2.5 in prod logs is overflow-by-design, not a misconfig. Upgrading the alt to `gemini-3-flash` is **gated on a latency benchmark**; deferred. See [[gemini-vc-router-rungs]] (memory).
