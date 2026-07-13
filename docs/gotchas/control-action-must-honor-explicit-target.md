---
type: gotcha
status: active
date: 2026-07-11
tags: [music, brain, routing, loop, autoplay]
related: [[autoplay-request-enables-loop-one]] [[bare-stop-command-misrouted-to-autoplay]] [[music-player-architecture]]
---

# A control action that carries an explicit target must SET it, not blind-toggle

## Trigger

A music control that has both a **button** (bare intent, "advance to the next
state") and a **voice/text** path (explicit target, "set state X") — e.g.
`loop`. If the handler always toggles/cycles, the voice/text target is silently
discarded and the user gets the *wrong* state — often the opposite of what they
asked for, with no way to correct it (each "turn it off" just toggles again).

Concretely: `{action: loop, mode: off}` used to call `cycle_loop_mode()`,
cycling `OFF → LOOP_ONE`. "Turn off loop" turned loop *on*. See
[[autoplay-request-enables-loop-one]].

## Why it happens

The button was built first (`{action: loop}`, no args → cycle). Voice/text was
bolted onto the same handler later and passes an explicit `mode`, but the
handler never read it. The 🔁 button and a spoken "loop off" are *different
contracts* sharing one action name.

## Don't

- Don't call `cycle_*()` / toggle unconditionally in a handler that voice/text
  can reach with an explicit target.
- Don't advertise a `mode`/`value` for the action in the tool schema and then
  ignore it in the handler — the router will faithfully pass it and it vanishes.
- Don't map "off" to "next mode." Off means off.

## Do

- Read the explicit target (`mode`, and `value` as a fallback — the model
  sometimes packs it there). Resolve it to a concrete state and **set** it.
- Fall back to cycle/toggle **only when no target is given** (the button).
- Keep `status` a pure read (no mutation).
- Give every distinct target an anchor in the routing prompt AND a documented
  schema value, so a weak fallback rung can express it (a missing anchor is how
  autoplay and loop got collapsed into each other — same lesson as
  [[bare-stop-command-misrouted-to-autoplay]]).

## Reference

- [music_cog.py](../../src/poob/discord_bot/cogs/music_cog.py) — `loop` action:
  `_LOOP_MODE_TARGETS` set-when-given / cycle-when-bare.
- [brain/poob.py](../../src/poob/brain/poob.py) — `_MUSIC_ROUTING_RULES`
  autoplay-vs-loop block, `mode` schema enum, `_LOOP_OFF_PHRASES` safety net.
- Incident: [[autoplay-request-enables-loop-one]].
