---
type: incident
status: resolved
date: 2026-07-09
tags: [brain, routing, music, tool-calling, gemini]
related: [[bare-stop-command-misrouted-to-autoplay]] [[tool-hallucination-from-passive-context]]
---

# "autoplay turn ON" routed to `apply_effect` (faster/more) instead of autoplay

## Symptom

User: "@Poob autoplay turn ON" (text channel). Poob replied "Now applying:
heavy bass, 1.5x speed, overload." — a completely unrelated effects action.

## Root cause — same disease as [[bare-stop-command-misrouted-to-autoplay]], different word

```
poob.tool_route args="{'action': 'apply_effect', 'effect': 'faster', 'mode': 'more'}" model=gemini-2.5-flash-lite provider=gemini tool=music_assistant
```

Immediate passive context: **7 `apply_effect` calls in the preceding ~9
minutes** (bass boost, ultrabass, faster, nightcore, dank, slowed, overload —
heavy effects-chaining chatter in an active voice session). `autoplay` had
**zero explicit routing-rule example** — the routing prompt's `mode` schema
description ("For 'autoplay': on/off/status") only helps *after* the model
has already decided `action=autoplay`; it does nothing to anchor the decision
itself. Faced with a heavily apply_effect-biased context and an unanchored
action, `gemini-2.5-flash-lite` continued the dominant recent pattern instead
of recognizing "autoplay" as its own action — textbook
[[tool-hallucination-from-passive-context]].

**This was foreseeable and should have been caught the first time.** The
prior night's incident ([[bare-stop-command-misrouted-to-autoplay]]) fixed
`stop` — one specific word — and explicitly deferred everything else
("skip/pause/resume were NOT given the same... coverage... if observed
failing, extend then"). That scoping missed that `autoplay` itself had the
exact same gap, and wasn't even in the set of actions considered at the time
(the prior incident was scoped to *basic transport controls*; autoplay is a
different action class — a mode toggle — that never got audited at all).

## Fix — this time, audit ALL actions, not just the reported one

1. **Full audit of `_MUSIC_ROUTING_RULES` against the 28-action schema
   enum.** ~20 actions had zero explicit routing example. Triaged by
   collision risk with the heavily-exampled `apply_effect`/`play` domains:
   - **High-risk, added:** `autoplay` (proven incident), `queue`/`remove`
     (word overlap with the effect-clearing phrasing already carved out:
     "remove the effect"/"clear effect"), `seek`.
   - **Low-risk, deliberately deferred:** `shuffle`/`loop`/`now_playing`/
     `clear` (schema already calls these "self-explanatory" and they don't
     share vocabulary with apply_effect); `move`/`previous`/`replay`/
     playlists/`queue_spotify_playlist`/`lyrics`/`leave`/`queue_many`
     (distinct enough vocabulary, no plausible collision path, cut for
     token budget — see below). If any of these are observed failing the
     same way, extend coverage then, same evidence-first discipline as
     before — but audited and triaged, not skipped by omission this time.

2. **Token budget forced real trade-offs, made explicitly, not by accident.**
   `_MUSIC_ROUTING_RULES` is duplicated into both the full persona prompt and
   the slim routing-only prompt (`test_routing_prompt_is_substantially_shorter`
   requires slim < 0.6× full) — every char added to routing rules costs
   budget on BOTH sides, so growth is expensive per the existing
   `test_music_tool_stays_under_budget`/slim-prompt tests. Final size ~4171
   chars (was ~3838 before `stop`'s fix, ~4234 after autoplay's prose alone
   before trimming to fit).

3. **Deterministic safety net, same proven pattern as `_BARE_STOP_PHRASES`:**
   added `_BARE_AUTOPLAY_PHRASES` (a phrase→mode dict, since unlike `stop`
   this command has three real outcomes: on/off/status) to `_music_safety_net`.
   Covers `"autoplay on"`, `"autoplay turn on"`, `"turn on autoplay"`,
   `"enable autoplay"` and the off/status equivalents — including the EXACT
   reported phrasing ("autoplay turn on"). Overrides whatever any rung
   routed, same as the stop guard, for the same reason: no other plausible
   reading of these exact phrases.

## Validation

New tests in `tests/unit/test_routing_rules.py`: the exact prod regression
(`"autoplay turn ON"` routed to `apply_effect` → corrected to
`autoplay`/`on`), on/off/status phrase variants, case/punctuation
insensitivity, a no-op check when routing already got it right, and a check
that `"turn off the autoplay filter thing"` (NOT an exact bare phrase) is
left alone — same anti-overreach discipline as the stop fix. Routing-prompt
budget tests re-verified green after trimming.

## Lesson for next time

When a routing-hallucination incident gets fixed for one action, the
FOLLOW-UP step is a full audit of the schema's action enum against the
routing-rules text — not "wait for the next specific word to fail in prod."
The `_BARE_STOP_PHRASES`/`_BARE_AUTOPLAY_PHRASES` pattern is cheap and
proven; extending it reactively one word at a time is what "failure after
failure" looks like from the outside, even when each individual fix is
correct and well-tested.
