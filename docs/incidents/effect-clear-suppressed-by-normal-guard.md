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

## Fix — two layers, one shipped

1. **Deterministic override (shipped)** — effect phrases added to
   `_CONTROL_OVERRIDES`, generated from templates × nouns rather than
   hand-listed. See "What actually shipped" below for the final shape; the
   first two attempts were both wrong and are recorded so the reasoning is
   not lost.
2. **Prompt (deferred)** — narrow the guard clause from "do NOT fire just
   because the word 'normal' appears" to the case it was written for:
   *volume*. Needs `MUSIC_TOOL` token headroom
   (`test_music_tool_stays_under_budget`, 1352/1400) first. This is the layer
   that actually closes the reported utterance.

**Both directions must stay pinned in tests** — a one-directional test is
what let this ship in the first place. `normal volume → volume` was tested;
`back to normal → effect cleared` was not, and that is the direction that
broke.

### What review caught — the fix was over-reaching and got scoped back

The first implementation **passed its own tests and did not fix the
incident.** It matched `"put the bass back to normal"` while the real
utterance was `"Hey, Poob. Put the base back to normal. This isn't good."`,
and control overrides match the whole message exactly, so the trailing clause
defeated it. The tests passed only because they were written against a
tidied paraphrase of the logged string rather than the string itself.

The obvious repair — reuse `_trim_trailing_crosstalk` to also try the first
sentence — was implemented and then **reverted**, because an adversarial
review proved it unsound in three separate ways:

- It applied to **all 11 override groups**, not just effect-clear. *"Turn it
  up. Actually turn it down."* force-fired `volume_up` on the retracted first
  clause.
- *"Hey Poob, no more bass. Play some jazz."* matched the first clause and
  **silently dropped the play request.**
- It only helped when the command **led** the utterance, so the mirror
  phrasing (*"This isn't good. Put the base back to normal."*) still failed.

And the guard test written for it **passed vacuously** — none of its phrases
contained an interior sentence terminator, so the trim never ran. That is the
same defect as the original: a test that cannot fail.

A second, worse over-reach was also caught. The generated set mapped **named**
effects (`remove the reverb`, `turn off the nightcore`) to
`effect='none'` — clear-**all**. Because `_music_safety_net` returns the
forced args unconditionally, that both wiped a deliberately-built stack and
**overwrote a route the LLM had already got right** (`mode='remove'`), which
is precisely what this override exists not to do. Verified end-to-end against
the real player: `nightcore + reverb` → `"remove the reverb"` → everything
gone, acked `[SILENT]` so the user hears nothing.

## What actually shipped

- **Generic** nouns (`effect(s)`, `filter(s)`) → `effect='none'` (clear all).
- **Named** effects (`bass`/`base`→`bassboost`, `nightcore`, `slowed`,
  `reverb`, `8d`, `tremolo`, `vibrato`) → `{effect: <id>, mode: 'remove'}`,
  dropping only that dimension and preserving the stack, per
  [[music-effect-stacking]]. A correct LLM `mode='remove'` route now passes
  through untouched — test-pinned.
- Matching stays **exact-whole-message**. The revert is recorded in a comment
  at the call site so the next person does not re-attempt it blind.

## Still open — stated plainly

**The verbatim production utterance is NOT fixed by this override.** *"Put
the base back to normal. This isn't good."* still returns `None`, and that is
pinned by `test_trailing_crosstalk_form_is_a_documented_open_gap` so the
limitation is visible instead of assumed away.

What ships is strictly better — the bare command now works deterministically,
and named removal no longer nukes the stack — but the exact reported
utterance still depends on the router. Closing it properly belongs to the
deferred **prompt** half (narrow the `'normal'` guard clause to volume), which
needs `MUSIC_TOOL` token headroom first. Widening the deterministic matcher is
the wrong lever; review demonstrated that concretely.

## Detection note

This was found by a **pre-deploy baseline audit** of the night's logs, not by
a user report. The user never complained — they just stopped asking. That is
the argument for auditing sessions rather than waiting for reports: a request
answered with a joke instead of an action leaves no error, no warning, and no
trace except the absence of a `music.response` line.
