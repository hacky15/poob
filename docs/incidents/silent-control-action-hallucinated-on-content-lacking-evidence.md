---
type: incident
status: resolved
date: 2026-09-09
tags: [brain, routing, voice, tool-calling, music]
related: [[bare-wake-address-routes-hallucinated-tool]] [[control-command-misroute-by-weak-rung]] [[autoplay-misrouted-to-apply-effect]] [[tool-hallucination-from-passive-context]]
---

# Addressed check-ins with real (but non-actionable) content still hallucinated a silent control action

## Symptom

Live production audit, 2026-09-10 02:08-02:11 UTC, two occurrences within 20 minutes:

```
02:10:53  addressed: "It doesn't. What'd you say, Logan? Hey, Poob."
          poob.tool_route args={'action': 'apply_effect', 'effect': 'slowed', 'mode': 'add'}
          Response complete response= total_ms=1342          <- dead silence

02:11:11  addressed: "K. Hey, Poob. You alive there?"
          poob.tool_route args={'action': 'apply_effect', 'effect': 'slowed', 'mode': 'add'}   <- IDENTICAL args
          Response complete response= total_ms=1091          <- dead silence
```

Both utterances were addressed check-ins with real words after the wake
phrase — neither is content-free in the sense
[[bare-wake-address-routes-hallucinated-tool]] already guards
(`_is_content_free`), which only catches a wake phrase reduced to under 2
characters after stripping. "You alive there?" is 14 characters of real
content; it just isn't a command.

## Root cause

`_is_content_free` only catches the *zero*-content case. It has no
counterpart for *non-zero, non-actionable* content — a real question or
check-in that carries no playback-control semantics at all. With music
playing, the routing prompt's music-context block unconditionally directs
the router to call `music_assistant` for anything playback-related; handed
an ambiguous, content-bearing-but-uncommand-like message, the router (or a
weak fallback rung) defaults to re-emitting the most recent real tool call
from a few turns earlier rather than recognizing there's nothing to route.
Because `apply_effect`/`skip`/`pause`/etc. are `[SILENT]` control acks
(no spoken confirmation by design), the hallucinated repeat executes with
zero audible feedback — indistinguishable, from the user's side, from the
bot being completely dead.

Same failure family as [[bare-wake-address-routes-hallucinated-tool]] (2026-07-11,
bare "Hey, Poob." → stale `autoplay/on`) and the misrouted-play override in
`_music_safety_net` (2026-07-16, explicit "Play X" → stale `apply_effect`)
— but neither existing guard covers this middle case: real content present,
message is not a play request, and the routed action is a *different*
stateful control verb with nothing in the message supporting it.

## Fix

Extended `PoobBrain._music_safety_net` with a fourth check — the
silent-control veto — run after the misrouted-play override, before the
function's final pass-through:

- `_SILENT_CONTROL_ACTIONS`: the `music_assistant` actions with no
  free-text argument to independently verify (`skip`, `pause`, `resume`,
  `stop`, `volume`/`volume_up`/`volume_down`, `shuffle`, `loop`, `move`,
  `remove`, `clear`, `apply_effect`, `seek`, `autoplay`,
  `save_playlist`/`load_playlist`/`delete_playlist`, `leave`). Deliberately
  excludes `play`/`queue_many`/`queue_spotify_playlist` (carry their own
  query/url as evidence, and are covered by the misrouted-play override
  instead) and read-only actions (`now_playing`, `list_effects`,
  `list_playlists`, `lyrics`, `queue`) that are harmless even if
  imprecisely routed.
- `_CONTROL_ACTION_KEYWORDS`: a single shared keyword list (hoisted from
  what was a local tuple inside `_groq_with_tools`'s cascade-continuation
  check, so the two checks can't drift apart) — play, skip, pause, volume,
  effect names, autoplay, restore, etc.
- If the routed action is in the silent set and **none** of the keywords
  appear anywhere in the current message (wake-prefix and
  speaker-attribution stripped, same variants `_match_control_override`
  already checks), the tool call is vetoed to `(None, None)` — falls
  through to a normal casual reply instead of executing silently.

No response-length or persona-prompt change — this is purely a
routing-layer veto, independent of how long Poob's replies run.

## Validation

- `tests/unit/test_routing_rules.py`: both exact prod transcripts
  reproduced and asserted vetoed; all 16 silent-control actions covered by
  a parametrized case; a control action WITH real supporting language
  (e.g. "add a bit more reverb") confirmed NOT vetoed; play/read-only
  actions confirmed never touched; deal routes confirmed untouched.
- Mutation-tested: disabled the veto condition, confirmed all 18 new tests
  failed with the expected mismatch, restored, confirmed all pass.
- Full `test_routing_rules.py` (121 tests) and
  `test_provider_circuit_breaker.py` (25 tests) green — no regression in
  any existing control-override, autoplay/loop, or misrouted-play test.
- Not yet deployed — implemented and verified locally, not merged per
  operator instruction.

## Follow-ups

- The underlying reason the router reaches for a stale repeat at all
  (rather than a clean NO_TOOL) is a modeling/prompting question, not
  addressed here — this fix is a safety net at the execution boundary, not
  a routing-accuracy improvement.
- The "random double queue" reported the same night (two users' audio
  streams producing the identical transcript ~100ms apart, e.g. "Wow. Hey,
  Poob. Listen to natural potato.") was investigated and found to be
  physical mic bleed between users in the same room — Discord genuinely
  delivers two independent, correct audio tracks. Per operator instruction,
  this is NOT a bug and no fix was implemented for it.
