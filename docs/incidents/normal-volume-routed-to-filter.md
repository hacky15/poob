---
type: incident
status: resolved
date: 2026-06-09
tags: [brain, music, routing, prompt, volume, effects, gemini]
related: [[music-routing-flip-a-coin-false-positive]] [[provider-circuit-breaker]] [[groq-failfast-client]]
---

# "Normal volume" routed to a filter instead of setting the volume

## Symptom

Operator: *"I ask it to normal volume and it just starts messing with filters."*

Prod log (2026-06-09):
```
"Poob, normal volume"       → volume value=100         (groq)   ✅
"Poob, normal volume"       → apply_effect effect=none (gemini) ❌
"Hey, Poob. Normal volume"  → apply_effect super_slowed (gemini) ❌❌
```
Groq routed it correctly; **Gemini** mis-routed it — clearing the effect, and
once hallucinating `super_slowed`. It surfaced now because the
[[provider-circuit-breaker]] correctly shifts routing to Gemini while Groq's
daily cap is spent, exposing this Gemini-side weakness.

## Root cause

Self-inflicted. The effect-off fix ([[music-routing-flip-a-coin-false-positive]]
era, `9ef079f`) added **bare `'normal'`** to the apply_effect(none) trigger list
in `_build_system_prompt`:
```
'remove X' / 'turn off X' / 'clear effect' / 'no effect' / 'normal' → effect='none'
```
"normal" is overloaded — it's also a *volume* word ("normal volume"). The bare
keyword polluted the clean filter-vs-volume line, so the model (Gemini
especially) saw "normal" and jumped to the effect path, ignoring "volume". As
the operator put it: *"it shouldn't just be 'normal volume' that prompts it to
think volume — it should simply be straightforward filter vs volume."*

## Fix

Prompt-layer, principled (not a "normal volume" special-case):

1. **Removed bare `'normal'`** from the effect-clear trigger. Effect removal now
   keys on effect language only — "remove the effect", "turn off the filter /
   nightcore", "clear effect", "no effects", "back to normal **speed**" — plus an
   explicit *"do NOT fire this just because the word 'normal' appears"*.
2. **Stated the principle:** *"VOLUME IS NOT AN EFFECT — they are separate
   commands. Anything about LOUDNESS — 'normal volume' / 'max volume' / 'louder'
   / 'quieter' / 'turn it up/down' → action=volume … NEVER apply_effect. Effects
   are NAMED audio filters (nightcore, slowed, reverb, bassboost); volume is just
   how loud it is."*

## Validation

- `tests/unit/test_volume_vs_effect_routing.py`: bare-'normal' trigger gone,
  volume↔effect distinction present, effect-clear still works via specific
  phrasing, effect-on triggers unchanged.
- Full unit suite green.
- Post-deploy: "normal volume" → `volume value=100` on **both** Groq and Gemini;
  no `apply_effect` on loudness requests.
