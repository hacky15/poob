---
type: incident
status: active
date: 2026-07-27
tags: [brain, music, routing, effects, over-correction, prompt]
related: [[normal-volume-routed-to-filter]] [[control-command-misroute-by-weak-rung]] [[music-routing-prompt-thoughtfulness]] [[autoplay-request-enables-loop-one]]
---

# "Put the bass back to normal" → a joke, and the ultrabass stayed on

## Symptom

2026-07-27 04:26–05:14, voice. The user applied a heavy bass effect, disliked
it, and asked for it to be undone:

```
04:26:57  wake addressed  text="Hey, Poob. Turn the base up, like, extremely high."
04:26:58  poob.tool_route args={'action': 'apply_effect', 'effect': 'ultrabass'}
04:26:58  music.response  [SILENT]Now applying: heavy bass.

04:28:39  wake addressed  text="Hey, Poob. Put the base back to normal. This isn't good."
04:28:40  First sentence ready  sentence="you think the volume's a problem?"
04:28:44  Response complete     response="you think the volume's a problem? IT'S THE
                                          CONVERSATION THAT'S GOT ME S…"
```

**No `poob.tool_route`. No `music.response`. No `apply_effect`.** The request
was answered with a persona joke and the effect was never cleared.

The user did not retry. `ultrabass` stayed applied for the remaining ~45
minutes of the session. Worst-shape failure: asked once, got mocked, gave up.

## Root cause — an over-correction from a previous fix

[[normal-volume-routed-to-filter]] (2026-06-09) fixed the *opposite* bug:
Gemini was hijacking **"normal volume"** into `apply_effect(none)`, and once
hallucinated `super_slowed`. The fix added a guard clause to
`_MUSIC_ROUTING_RULES`:

```
- 'remove the effect' / 'turn off the filter' / 'clear effect' / 'no effects'
  / 'back to normal speed' → action=apply_effect, effect='none' (clears ALL
  effects). Do NOT invent an effect here, and do NOT fire this just because
  the word 'normal' appears.
```

That last clause is now doing the damage. **"Put the bass back to normal" is a
genuine effect-clear request whose most salient token is exactly the one the
prompt tells the router to distrust.** The 06-09 fix stopped `normal`→effect
false positives by teaching the model that `normal` is not evidence — which
also suppresses the true positives.

The enumerated triggers do not rescue it either: the list covers
`remove the effect` / `turn off the filter` / `clear effect` / `no effects` /
`back to normal speed`. "Put the **bass** back to normal" matches none of
them — the list is phrased around *speed* and *generic* effects, never around
clearing a **named** effect by naming it.

**And there is no deterministic backstop.** `_CONTROL_OVERRIDES` covers
stop, skip, max/mute volume, louder, quieter, autoplay on/off/status, and
loop — but has **no effect-clear entry at all**. So when the prompt layer
declines, nothing catches it, and the message falls through to casual chat.
This is the same shape as [[control-command-misroute-by-weak-rung]], one rung
lower: there the deterministic net existed and could not see past the wake
prefix; here it simply does not cover this action.

## Why this matters beyond the one event

This is a clean instance of the pattern the operator has been complaining
about — *"every time we fix an issue another one comes up."* The 06-09 fix
was correct and well-documented; it just traded a false-positive for a
false-negative in the same sentence, and nothing tested the other direction.
`normal-volume-routed-to-filter` has validation for "normal volume → volume"
but none for "back to normal → effect cleared".

## Fix — proposed, NOT yet implemented

Two layers, mirroring how every other control command in this codebase is
handled:

1. **Deterministic override** — add an effect-clear entry to
   `_CONTROL_OVERRIDES` (`{"action": "apply_effect", "effect": "none"}`)
   keyed on an exact-phrase set in the established
   `_BARE_*_PHRASES` style: "back to normal", "put it back to normal",
   "bass back to normal", "normal bass", "remove the bass", "turn off the
   bass", "no more bass". Exact-whole-message matching only, per the existing
   discipline — that is what keeps "normal volume" (a genuine volume command,
   and its own documented incident) from being swallowed.
2. **Prompt** — narrow the guard clause from "do NOT fire just because the
   word 'normal' appears" to the case it was actually written for: *volume*.
   Something like "'normal volume' / 'regular volume' is VOLUME, not an
   effect — but 'put X back to normal' where X is an effect (bass, speed,
   nightcore) IS effect='none'."

**Both must be evidence-pinned in tests in both directions** — that is the
specific gap that let this ship: `normal volume → volume` is tested,
`bass back to normal → effect none` is not. Whichever direction is left
untested is the one that regresses next.

Not implemented in this pass because `MUSIC_TOOL` is at 1352/1400 tokens and
the prompt half needs the de-duplication tracked in
`test_music_tool_stays_under_budget` first. The deterministic override half
has no such constraint and can land independently.

## Detection note

This was found by a **pre-deploy baseline audit** of the night's logs, not by
a user report. The user never complained — they just stopped asking. That is
the argument for auditing sessions rather than waiting for reports: a request
answered with a joke instead of an action leaves no error, no warning, and no
trace except the absence of a `music.response` line.
