---
type: incident
status: resolved
date: 2026-07-06
tags: [brain, routing, music, tool-calling, gemini]
related: [[groq-gpt-oss-20b-swap]] [[gemini-router-prefer-2.5-ga-over-3.1-preview]] [[tool-hallucination-from-passive-context]]
---

# A bare "stop." routed to `{action: autoplay, mode: on}` instead of stopping

## Symptom

User said "@Poob stop." (5 characters, no other content). Poob replied
"Autoplay enabled." instead of stopping the music.

## Root cause (two layers)

Log trace for the exact request:

```
poob.no_tool_but_tool_worthy_trying_next  model=openai/gpt-oss-20b provider=groq
poob.tool_route  args="{'action': 'autoplay', 'mode': 'on', 'name': 'stop'}" model=gemini-2.5-flash-lite provider=gemini tool=music_assistant
```

1. **Primary router (Groq) returned no tool at all** for the bare word "stop" —
   the code correctly detected this looked tool-worthy (music was playing) and
   fell through to the next provider rather than giving up.
2. **The fallback (`gemini-2.5-flash-lite`, our current primary Gemini rung —
   see [[gemini-router-prefer-2.5-ga-over-3.1-preview]]) hallucinated a
   bizarre tool call**: `action=autoplay, mode=on`, plus a `name: 'stop'` field
   that isn't even a valid schema parameter — the model apparently tried to
   encode the literal word "stop" as *something* but landed on a completely
   wrong action.

Why: the routing rules (`_MUSIC_ROUTING_RULES`) have a **large, detailed
example block for audio effects** (nightcore, reverb, darth vader, stacking
modes, adjust modes, list_effects...) but the basic control verbs — stop,
skip, pause — were only ever mentioned once, in a single run-on sentence
("play, queue, skip, pause, stop, control volume") with **no explicit
`action=X` example to anchor on**. A cheap/weak rung, faced with a terse
one-word input and no matching example, is more likely to pattern-match onto
the heavily-exampled autoplay/effects space nearby in the prompt than to
correctly infer the obvious mapping.

## Fix (two layers, matching root cause)

1. **Prompt-level (reduces how often this needs a fallback at all):** added an
   explicit `BASIC CONTROLS` example block right after the routing intro —
   `'stop' → action=stop`, `'skip' → action=skip`, `'pause' → action=pause`,
   deliberately NOT touching the existing `unpause`/`resume` → `action=restore`
   rule (which already correctly handles both "paused" and "not paused" cases
   internally — an earlier draft of this fix mistakenly tried to add a second,
   contradictory resume rule; caught and reverted before shipping).

2. **Deterministic safety net (the robust backstop, since prompt-following is
   never 100% guaranteed with a cheap fallback model):** extended
   `_music_safety_net` — which already has a proven, narrow "catch obvious
   misses" pattern for `play` intent — with a **bare-stop-phrase override**.
   If the ENTIRE message (nothing else) is one of `{"stop", "stop it", "stop
   the music", "stop the song", "stop playing"}`, the action is forced to
   `stop` regardless of what any rung returned — this is the one case where
   the safety net overrides an *already-present* tool call, not just a
   missing one, because there is no other plausible reading of the exact
   phrase.

   Deliberately narrow: no `"shut it off"` or similar (has non-music
   readings), and only the *entire* message, not a substring — `"stop the
   effects please"` is untouched and keeps whatever the LLM routed (verified
   by test), since it has a different, real distinction (`apply_effect`
   vs `stop`) the narrow list must not swallow. Worst-case false positive
   (a truly unrelated "stop." with nothing playing) is harmless: `action=stop`
   always returns `"[SILENT]Music stopped and queue cleared"` even with an
   idle queue.

## Validation

`tests/unit/test_routing_rules.py`: the exact prod regression (bare `"stop."`
routed to `autoplay/on` → corrected to `stop`), the no-tool-at-all case, 8
phrase variants (case/punctuation/whitespace), a no-op check when the LLM
already got it right, and — critically — a check that `"stop the effects
please"` is left alone (proving the fix doesn't overreach). Full suite green.

## Not done here

`skip`/`pause`/`resume` were NOT given the same deterministic override — only
the prompt-level example. There's no observed evidence (yet) that those are
similarly misrouted; adding an unproven safety net for them would be premature
scope creep. If skip/pause are observed failing the same way in prod, extend
`_BARE_STOP_PHRASES`-style coverage to them then, with the same evidence-first
discipline.
