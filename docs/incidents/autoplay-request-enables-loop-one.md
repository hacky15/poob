---
type: incident
status: resolved
date: 2026-07-11
tags: [music, brain, routing, loop, autoplay, player]
related: [[bare-stop-command-misrouted-to-autoplay]] [[stop-does-not-disable-autoplay]] [[music-player-architecture]] [[music-autoplay-cascade]] [[control-action-must-honor-explicit-target]]
---

# Asking for autoplay silently enabled LOOP_ONE — same song looped forever

## Symptom

Operator, live: *"we have asked for other songs and requests but it keeps
looping the same one. is it stuck on loop? we didn't ask for loop... we asked
for autoplay."* Newly-requested songs never played — the current track
repeated indefinitely. The user fought this across three sessions (07‑09 →
07‑11) and autoplay never once turned on.

Production `voice.log` trace (the decisive one, 2026‑07‑11):

```
00:47:40  transcript = "Hey, Poob. Turn autoplay on."
00:47:42  poob.tool_route  args={'mode': 'off', 'action': 'loop'}   provider=gemini
00:47:42  music.response   "[SILENT]Loop mode: looping current track."
```

The user said *autoplay on*; the bot enabled *LOOP_ONE*. In `LoopMode.LOOP_ONE`,
`MusicQueue.get_next()` returns the current track and never pops the queue, so
every subsequent "play X" queued a track that never advanced.

## Root cause (three stacked defects)

1. **The `loop` handler ignored its own argument.** `MusicCog.handle_music_request`
   called `player.queue.cycle_loop_mode()` unconditionally, discarding the
   `mode`/`value` the router passed. So `{loop, mode: off}` did **not** set loop
   off — it cycled to the *next* mode (`OFF → LOOP_ONE`, `LOOP_ONE → LOOP_QUEUE`).
   The log shows the cycle repeatedly landing on a looping mode when the user
   asked for "off":

   | routed | loop was | cycle produced | user wanted |
   |---|---|---|---|
   | `{loop, mode: off}` | OFF | LOOP_ONE | off |
   | `{loop, value: off}` | OFF | LOOP_ONE | off |
   | `{loop, mode: off}` | LOOP_ONE | LOOP_QUEUE | off |

   This is also why the user could never turn loop *off* once it was on —
   every "turn off loop" just cycled to another looping mode.

2. **The router confused `autoplay` and `loop`.** `_MUSIC_ROUTING_RULES` had
   detailed examples for play/stop/skip/pause/effects/volume but **zero
   examples for autoplay or loop**. With nothing to anchor on, the cheap
   `gemini-2.5-flash-lite` fallback pattern-matched "turn autoplay on" onto
   `{action: loop, mode: off}`. Identical failure class to
   [[bare-stop-command-misrouted-to-autoplay]].

3. **The schema gave `loop` no way to express a target mode.** The `mode` enum
   was documented "For 'autoplay': on/off/status"; `loop` was listed as
   "self-explanatory" with no parameter. So even a well-behaved model could
   only borrow autoplay's `mode: off` and attach it to `loop` — which defect 1
   then discarded.

Net effect: autoplay never turned on (bare `{autoplay}` → "on/off/status?";
"turn autoplay on" → loop; "apply the autoplay…" → an effect), and loop kept
getting flipped on with no reliable way off.

## What it was NOT

- **Not "looping on start."** Fresh players start `LoopMode.OFF`
  (`MusicQueue.__init__`).
- **Not cross-session state.** `loop_mode` lives on the per-guild player;
  `_destroy_player` (VC disconnect) and `stop()`/`clear_all()` reset it. It only
  *felt* sticky because defect 1 made LOOP_ONE un-turn-off-able within a live
  session.

## Fix (all three layers, additive)

1. **Handler honors the explicit target** ([music_cog.py](../../src/poob/discord_bot/cogs/music_cog.py)):
   a new `_LOOP_MODE_TARGETS` map resolves `off/one/queue` (+ synonyms) from
   `mode` (or `value`, since the model sometimes packs the target there —
   `{loop, value: 'off'}` was seen in prod). `status` reports without mutating.
   **Absent or unrecognized input still falls back to `cycle_loop_mode()` — the
   behavior the 🔁 now-playing button relies on (it sends `{action: loop}` with
   no mode).** Cycling was correct *for the button*; it was only wrong for
   voice/text, which carry an explicit intent.
2. **Routing rules** ([brain/poob.py](../../src/poob/brain/poob.py)): an explicit
   "AUTOPLAY vs LOOP are DIFFERENT" block with `action=autoplay, mode=on|off|status`
   and `action=loop, mode=one|queue|off` anchors. Kept tight so the slim routing
   prompt stays under its length guard.
3. **Schema**: `mode` enum gains `one`/`queue`; its description and the `action`
   description now document the autoplay-vs-loop distinction.
4. **Deterministic safety net** (belt-and-suspenders): `_music_safety_net` forces
   the right control for the exact unambiguous phrases (`_AUTOPLAY_ON_PHRASES`,
   `_AUTOPLAY_OFF_PHRASES`, `_LOOP_OFF_PHRASES`), overriding even a present
   misrouted tool — same narrow, whole-message discipline as the bare-stop
   override. (As with bare-stop, this fires on the paths where the router
   returns no tool; defect‑1 + the routing rules are what fix the observed
   present-but-wrong-tool cases.)

The linchpin is defect 1: once the handler honors the target, even a *misrouted*
"turn autoplay on" → `{loop, off}` now sets loop **off** (harmless) instead of
cycling it on.

## Validation

- `tests/unit/test_music_handler_actions.py`: loop off/one/queue set the exact
  mode; "off while already OFF" stays OFF (the prod bug); `value` accepted as a
  target; bare `{loop}` still cycles OFF→ONE→QUEUE→OFF (button preserved);
  `status` doesn't mutate; unrecognized mode falls back to cycle; schema
  advertises `one`/`queue`.
- `tests/unit/test_routing_rules.py`: safety net forces autoplay on/off and
  loop-off (including over a misrouted `{loop, off}`/`{loop}`); no false
  positives on "play autoplay by …" / "loop me in"; routing rules carry the
  autoplay/loop anchors.
- Full unit suite green.
- Post-deploy log signals: "turn autoplay on" should show
  `poob.tool_route args={'action': 'autoplay', 'mode': 'on'}` and
  `[SILENT]Autoplay enabled.`; "turn off loop" should show
  `[SILENT]Loop mode: off.` (never "looping current track").

## Follow-ups

- The general hazard — a control action that ignores an explicit target and
  blindly toggles — is captured in [[control-action-must-honor-explicit-target]].
- ~~The safety net only fires on the router-returned-no-tool paths (call sites
  gate on `not tool_name`).~~ **Done same day**: the "max volume" → skip
  misroute proved present-but-wrong tools DO reach users, so the net now runs
  unconditionally and is wake-prefix-aware. See
  [[control-command-misroute-by-weak-rung]].
