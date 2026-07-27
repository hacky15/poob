---
type: incident
status: resolved
date: 2026-07-21
tags: [voice, discord, pycord, reconnect, observability, silent-failure]
related: [[voice-auto-rejoin-on-restart]] [[zombie-voice-connection-blocks-autojoin]] [[voice-4014-reconnect-event-loop-wedge]] [[per-subsystem-component-logging]] [[no-ci-test-gate]]
---

# Poob went deaf in a guild for 73 hours and nothing said a word

## Symptom

Not user-reported — **found by audit**, which is the whole point of this
note. During the 2026-07-26 production sweep, `data/voice_state.json` still
declared Poob was in `Kittens Playhouse`:

```
{"702353477602377769": 889223647674384414}
```

It had not been in that channel since **2026-07-21 05:47**. No wake word, no
music, no commands, in that guild, for 73+ hours. Zero errors surfaced to
users or the operator. The container never restarted (`Up 5 days`,
`RestartCount=0`), so nothing self-healed it.

## Root cause

A voice WS 1006 dropped the connection and **py-cord's own retry loop
exhausted**:

```
05:47:07  [VoiceCompat] Voice websocket closed: code=1006 type=257
05:47:08  Connecting to voice... (connection attempt 2)
05:47:23  The voice handshake is being terminated for Channel ID 889223647674384414
05:47:23  Could not connect to voice... Retrying...
05:47:23  Disconnecting from voice normally, close code 1000.
```

Then nothing, ever again. The other guild (`770491927580639295`) hit the
same 1006 in the same second, succeeded on attempt 2, and kept serving
traffic — the only difference was whether py-cord's internal retry happened
to win.

Poob's own layer had no answer for "py-cord gave up":

- `restore_sessions()` is the **only** consumer of `VoiceStateStore`. It is
  `_restored`-guarded and called once from `on_ready`.
- The gateway RESUMEd successfully, so `on_ready` never re-fired — and
  `_restored` would have blocked it anyway.
- `on_voice_state_update` early-returns when the session is not connected,
  so even a user rejoining the channel triggered nothing.
- The persisted entry was never cleared either (the final `1000` close comes
  from discord.py internals, not `_force_disconnect`), so intent and reality
  diverged permanently with **no loop anywhere that compares them**.

[[voice-auto-rejoin-on-restart]] designed exactly this policy — *"transient
connect/setup failure → keep persisted and retry on the next restart"* — and
that is correct. The gap is that it silently assumes there **will be** a next
restart. Between restarts, nothing retries.

## Fix

A periodic reconciler in `VoiceCog` (`_reconcile_voice_presence`,
`VOICE_RECONCILE_INTERVAL_SEC = 120`) that compares persisted membership
against live state and repairs the difference, reusing the same
`setup_session_for_vc` chokepoint `/join`, auto-join, and boot restore all
use:

- Healthy session → untouched (never disturb a live connection).
- Diverged → log a **warning** (this is the part that ends the silence),
  clear any zombie client, reconnect, re-run setup, log recovery.
- Channel gone → forget it (permanent, same as boot restore).
- Transient failure → keep persisted, retry next cycle, attempt counter in
  the log so a stuck guild is visible rather than inferred.

**The trap this fix had to avoid:** `_force_disconnect` prunes the store.
Using it naively to clear the zombie would have erased the very membership
the reconciler exists to restore — converting a recoverable outage into a
permanent one. It now takes `forget: bool = True`, and the reconciler is the
sole `forget=False` caller. Pinned by
`test_force_disconnect_still_prunes_the_store_by_default`.

Why this layer, explicitly:

- **Not the voice WS layer** — [[voice-4014-reconnect-event-loop-wedge]] and
  [[voice-auto-rejoin-on-restart]] already draw that boundary: the WS layer
  owns a live 4014; this is "the retry loop is over and we are simply gone."
- **Not `on_ready`/gateway** — the gateway was healthy throughout.
- **Not the scanner heartbeat** — [[proactive-health-heartbeat]] is
  explicitly scanner-scoped; voice presence had no monitor at all.

## Validation

`tests/unit/test_voice_presence_reconciler.py` (9 tests) — `restore_sessions`
previously had **zero** coverage, which is why the gap survived:

- the exact prod shape (persisted, no live session, no zombie) reconnects
- a healthy session is never disturbed
- a zombie is cleared with `forget=False` and membership preserved
- `_force_disconnect` still prunes by default (regression guard for `/leave`)
- a deleted channel is forgotten; transient failures keep membership
- connected-but-setup-failed disconnects rather than sitting there deaf
- the loop is stoppable so it cannot outlive the cog

## The real lesson

The outage lasted 73 hours because it was **silent**, not because it was
hard to fix. The failure had no error, no alert, no log line saying
"I should be somewhere I am not." Every other finding in the 07-26 audit
shares that shape, which is why the same change also ships
[[per-subsystem-component-logging]] and a CI test gate ([[no-ci-test-gate]]).

Poob does not need to self-heal everything. It needs to **say when it
can't** — a bug you can see is an afternoon; a bug you can't is three days
and a furious operator.

## Follow-ups

- The reconciler repairs *persisted* membership only. A guild Poob was never
  recorded in is out of scope by design (that is `/join`'s job).
- Consider surfacing repeated reconcile failures to the operator channel;
  right now they are warnings in the log, which is a strict improvement over
  silence but still pull-based.
