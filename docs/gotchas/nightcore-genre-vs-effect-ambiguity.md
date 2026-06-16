---
type: gotcha
status: active
date: 2026-06-15
tags: [brain, routing, music, effects]
related: [[slim-tool-schemas]] [[music-routing-prompt-thoughtfulness]] [[music-filter-presets]]
---

# "play some nightcore" mis-routes: genre-to-PLAY vs effect-to-APPLY

## Hazard

`"play some nightcore"` (and similar: "play some vaporwave", "put on slowed
music") is ambiguous to the router because the word is **both**:

- a music **genre/style** the user wants to *hear* (→ `music_assistant` `play`,
  query="nightcore"), and
- an audio **effect preset** in the registry (`nightcore`, `vaporwave`,
  `slowed`) the user could *apply* to the current track (→ `apply_effect`).

Observed (2026-06-15 routing benchmark, BOTH full and trimmed schemas — this is
**not** a schema-trim regression, it predates it):

- Gemini-3.1 → `apply_effect: nightcore` (treats it as an effect to apply)
- Groq gpt-oss-20b → `NO_TOOL` or inconsistent

The correct route for "**play** some nightcore" with nothing playing is `play`
(the user wants to hear that style), not `apply_effect`.

## Why it's tricky

The effect registry deliberately reuses genre names (`nightcore`, `slowed`,
`vaporwave`) because those ARE the TikTok-genre effects. So the overlap is
inherent. The disambiguator is the **verb + playback state**:

- "**play** / put on / queue [X]" → `play` (X is what to hear), even if X names
  an effect — *especially* when nothing is playing.
- "**make it** / apply / add [X]" or "[X] **it**" while music plays →
  `apply_effect`.

## Do (proposed fix — not yet shipped)

Add one disambiguation line to the routing guidance (prompt or the `action`
description): *"'play [X]' means PLAY X as music even if X is also an effect
name (nightcore/slowed/vaporwave); only use apply_effect when the user says
apply/add/make-it or names an effect while music is already playing."* Then
re-run `tests/manual/bench_router_providers.py` — the `play some fuckin
nightcore` MATRIX case must flip MISS→OK without regressing the effect cases
("slow it down and reverb" must still route to apply_effect).

## Don't

- Don't remove genre-named effects from the registry — they're the whole point
  of the slowed/nightcore presets.
- Don't hardcode a "nightcore→play" special-case in code — fix it as routing
  guidance so it generalizes to vaporwave/slowed/etc.
