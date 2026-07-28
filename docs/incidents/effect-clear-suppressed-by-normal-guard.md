---
type: incident
status: resolved
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
2. **Prompt (shipped 2026-07-28)** — the guard clause is narrowed to the case
   it was written for. See "Prompt fix" below.

   **Correction:** this was first recorded as blocked on `MUSIC_TOOL` token
   headroom. That was wrong — the clause lives in `_MUSIC_ROUTING_RULES`
   (the system prompt), which the `MUSIC_TOOL` budget does not gate at all.
   The real constraint was
   `test_slim_routing_prompt.py::test_routing_prompt_is_substantially_shorter`
   (`len(slim) < 0.6 * len(full)`), which had **3.6 characters** of slack —
   roughly 9 addable characters, since text added to the shared rules grows
   both sides. Recorded because "blocked on X" claims get repeated, and this
   one was repeated three times before anyone measured it.

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

## Corroborating evidence — the gap recurred within 24 hours

2026-07-28 03:44, post-deploy, a **second** instance of the same shape — this
time on `stop`, not an effect:

```
03:44:02  "Hey, Poob. Stop playing"                                    -> action=stop        (override hit)
03:44:10  "Hey, Poob. Stop playing this shit. Where are you, Jackson?" -> NO tool routed
          -> conversational reply: "i'm right here, hacky15 ... i stopped the song a minute ago"
```

Verified against the **live deployed code**: the first form returns
`{"action": "stop"}`, the second returns `None`. The only reason there was no
user harm is that the music had already been stopped 8 seconds earlier, so the
casual reply happened to be true.

This is the same failure as the bass incident with a different verb, so the
gap is **not effect-specific — it is a property of exact-whole-message
matching meeting real speech**. Two occurrences in 24 hours across two
different control actions is the evidence threshold this codebase asks for
before extending a phrase-matching behaviour.

It also re-confirms that widening the matcher is the wrong lever (review
already proved that concretely), which leaves the deferred **prompt** half as
the real fix: the router, not the phrase table, is what can read
"stop playing X" plus trailing crosstalk as a stop.

## Prompt fix (2026-07-28) — the layer that actually closes it

The deterministic override handles the bare command; the router is what can
read a command wrapped in real speech. The clause became:

```
- 'remove the effect' / 'clear effect' / 'no effects' → apply_effect,
  effect='none' (ALL). Naming one ('turn off the nightcore', 'bass back to
  normal') → that effect, mode='remove'. Never invent an effect;
  'normal volume' is VOLUME.
```

It is **shorter than what it replaced** — 233 chars / 64 tokens vs 280 / 75 —
so it needed no headroom at all; slack on the ratio guard went from 3.6 to
22.4 characters. Rewriting to be *more precise* turned out to cost less than
the blunt version, which is worth remembering the next time a prompt change
looks budget-blocked.

What it preserves, what it changes:

- Bare `'normal'` is still **not** a clear-all trigger — the 2026-06-09 false
  positive stays fixed, still test-pinned.
- Loudness is still explicitly VOLUME (`'normal volume' is VOLUME`, plus the
  untouched "VOLUME IS NOT AN EFFECT" block below it).
- **New:** naming an effect now routes to removing *that* effect
  (`mode='remove'`), matching [[music-effect-stacking]] and the deterministic
  override shipped alongside it — the two layers now agree instead of one
  being silent.

Two tests pinned the old wording as a literal string
(`test_bare_normal_is_no_longer_an_effect_clear_trigger` and the
`_MUSIC_RULE_SUBSTRINGS` sentinel list). Both were updated to assert the
**intent** — bare 'normal' absent from the trigger list, loudness routed to
volume, a named effect removable — rather than the exact sentence, so the
next precise rewording does not read as a dropped rule.

## The guard that guards the guards

The first version of the new prompt-layer regression test was **vacuous** —
the third such test written in this session. It asserted:

```python
assert "mode='remove'" in p
assert "back to normal" in p.lower()
```

Both were **already true before the change**: `mode='remove'` comes from the
untouched STACKING bullet, and `"back to normal"` from the old clause's own
`'back to normal speed'`. Reverting the fix left it green. Caught by review
via mutation testing, not by inspection — and notably not by the author's own
verification pass, which happened to check the *other* (non-vacuous) test.

Fixed by asserting substrings that **discriminate**: `"Naming one"`,
`"that effect, mode='remove'"`, `"bass back to normal"`.

And to stop this recurring a fourth time,
`test_effect_clause_guards_are_not_vacuous` now reconstructs the pre-change
clause and asserts every probe the other guards rely on is **absent** from
it — a test whose only job is to prove the other tests can fail. It also
pins the two original vacuous probes as documented counter-examples so they
are not reintroduced.

Verified by mutation, not assertion: reverting the clause to the 2026-06-09
wording fails four tests
(`test_bare_normal_is_no_longer_an_effect_clear_trigger`,
`test_naming_an_effect_routes_to_removing_that_effect`,
`test_effect_clause_guards_are_not_vacuous`,
`test_routing_prompt_keeps_every_music_routing_rule`).

**The generalisable lesson:** a regression guard written from the *new* text
tends to assert whatever is convenient, and convenient strings are often
already present. The only reliable check is to run the guard against the code
it is meant to reject. Mutation-test regression guards, or assume they are
decorative.

## Detection note

This was found by a **pre-deploy baseline audit** of the night's logs, not by
a user report. The user never complained — they just stopped asking. That is
the argument for auditing sessions rather than waiting for reports: a request
answered with a joke instead of an action leaves no error, no warning, and no
trace except the absence of a `music.response` line.
