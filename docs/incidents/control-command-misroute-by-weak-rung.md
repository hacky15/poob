---
type: incident
status: resolved
date: 2026-07-11
tags: [brain, routing, music, tool-calling, gemini, voice]
related: [[bare-stop-command-misrouted-to-autoplay]] [[autoplay-request-enables-loop-one]] [[control-action-must-honor-explicit-target]] [[wake-address-hey-dropout]]
---

# "Max volume" skipped the song — control commands misrouted by the weak rung, and the safety net couldn't catch them

## Symptom

Operator: *"sometimes you'd ask for it to speed up or slow down or skip or
something, and it would DO SOMETHING COMPLETELY DIFFERENT. the_gamer asked to
make it louder, and it skipped the song."*

Production `voice.log`, 2026-07-11:

```
03:08:19  transcript = "Hey, Poob. Max volume."          user=Hour late (the_._gamer)
03:08:21  poob.tool_route  args={'action': 'skip'}       provider=gemini model=gemini-2.5-flash-lite
03:08:21  music.response   "[SILENT]Skipped 365 Days x Fifty Shades…"
00:48:18  transcript = "Hey, Poob. Apply infinite base boost to max volume."
00:48:21  poob.tool_route  args={'mode': 'add', 'effect': 'slowed', ...}   ← bass boost → "slowed"
```

Same failure family as [[bare-stop-command-misrouted-to-autoplay]] (bare
"stop." → autoplay/on) and [[autoplay-request-enables-loop-one]] ("turn
autoplay on" → loop/off): the weak fallback rung (`gemini-2.5-flash-lite`)
hallucinates an action for a terse control command. Groq (primary) tends to
return no-tool on these, the tool-worthy valve pushes to the next rung, and
the weak rung's wrong answer wins.

## Root cause — why the existing deterministic net was useless here

The bare-stop/autoplay/loop overrides added earlier could not catch this class
for two structural reasons:

1. **They only ran when the router returned NO tool.** Both call sites gated
   on `if not tool_name`. Here the cascade returned a *present, wrong* tool
   (`{action: skip}`), so the net was never consulted. (The bare-stop unit
   tests exercised the override-a-present-tool path by calling the method
   directly — production wiring never did.)
2. **They couldn't see past the wake prefix.** On voice, `clean_message`
   keeps the "Hey, Poob." head (`_split_context` strips the passive-transcript
   wrapper and the "Name said to you:" attribution, but not the spoken wake
   token), so an exact match on `"max volume"` failed against
   `"Hey, Poob. Max volume."`. The exact-phrase nets were effectively
   text-path-only — and every observed misroute came from voice.

## Fix

Unified deterministic **control override** in `PoobBrain` ([brain/poob.py](../../src/poob/brain/poob.py)):

- `_match_control_override(clean_message)`: strips a leading speaker
  attribution (`"Ben: …"`, the no-passive-transcript voice shape) and the
  wake/address token (`"Hey, Poob."`, including the documented phonetic
  mis-hears poop/boop/pube/pood), then requires the ENTIRE remaining message
  to exactly match a curated phrase set. Ordered table `_CONTROL_OVERRIDES`:
  stop, skip/next, max volume/mute (absolute `volume` with value 200/0),
  louder/quieter (`volume_up`/`volume_down`), autoplay on/off, loop off.
- `_music_safety_net` now applies the override **first**, replacing the
  separate bare-stop and autoplay/loop blocks (their phrase sets are reused
  verbatim inside the table). The play-intent backfill below it is unchanged
  and still never overrides a present tool.
- **Both call sites run the net unconditionally** (was `if not tool_name`),
  so a present-but-MISROUTED tool gets corrected. Non-control messages pass
  through untouched — the exact-whole-command match is the safety gate.

Why this shape: the misroute is a property of the cheap rung and will recur
with every new terse control verb; prompt examples reduce frequency but a
weak model's compliance is never guaranteed. For commands with exactly one
plausible reading, determinism beats another prompt tweak. Longer sentences
("skip the intro and play the chorus") never match and keep LLM routing.

Speed verbs ("faster"/"slower") were deliberately NOT added — they're
effect-flavored with more/less nuance; extend only with evidence, same
discipline as the bare-stop incident's "Not done here".

## Validation

- `tests/unit/test_routing_rules.py`: the exact prod case ("Hey, Poob. Max
  volume." routed `{skip}` → corrected to `{volume, 200}`); volume/mute/
  louder/quieter/skip phrase tables; wake- and attribution-prefixed matches;
  bare-stop now firing on the voice shape; longer sentences untouched with
  and without a present tool; play backfill behavior unchanged.
- Full brain/music suites green (routing, handler actions, boob variant,
  casual fallback, gemini router, multi-guild, UI).
- Post-deploy signals: a `Safety net overrode misrouted control command`
  WARN with `routed_args` showing what the rung hallucinated; "max volume"
  must produce `[SILENT]Music volume set to 200%.`, never a skip.

## Review-caught refinements (2026-07-13)

An adversarial multi-agent review of the integrated diff, before commit,
caught two real regressions this fix introduced by making the net
unconditional — both now fixed and tested:

1. **The unconditional net clobbered a correctly-routed `deal_assistant`.**
   During an active deal Q&A the router sends a terse answer ("stop"/"skip"/
   "next") to the deal agent (the ACTIVE DEAL SESSION hint pins follow-ups
   there). The override, matching those in `_BARE_STOP_PHRASES`/`_SKIP_PHRASES`
   regardless of the routed tool, rewrote them to a music action and silently
   dropped the deal command. **Fix:** the forced override now applies only when
   `tool_name in (None, "music_assistant")` — it still corrects a missing route
   and a music↔music misroute (max-volume→skip), but never a routed deal.
2. **`_SPEAKER_ATTRIBUTION_RE` over-stripped on the text path.** The regex
   `^\s*[^:\n]{1,40}:\s+` matched ANY short "word: " head, so ordinary TEXT
   chat whose tail is a control phrase ("note to self: skip this song",
   "fyi: next") got force-executed once the net went unconditional. The
   "Speaker: " head is only ever prepended on the voice-no-transcript path.
   **Fix:** attribution-stripping is now **voice-gated** (`_head_stripped_variants`
   applies it only when `voice=True`); `voice` is threaded through
   `_is_content_free` / `_match_control_override` / `_music_safety_net` from
   both call sites.

## Follow-ups

- Watch for misroutes on phrases *outside* the curated sets (e.g. speed
  verbs) and extend the table with evidence.
- The `_MUSIC_ROUTING_RULES` volume block already anchors volume routing;
  no prompt change shipped here (the override is the robust layer, and the
  slim-prompt length guard leaves little headroom).
- **Known minor wart (not a regression fixed here):** a bare control word
  ("next"/"louder") addressed to the bot with no music playing still forces a
  music action and `_handle_music` auto-joins the requester's VC to act on
  nothing. Pre-existed for bare "stop"; the expanded phrase set widens it
  slightly. Gating auto-join to audio-starting actions is a separate,
  reviewable change — deferred, not bundled.
