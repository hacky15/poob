---
type: incident
status: resolved
date: 2026-06-03
tags: [voice, deepgram, stt, reliability, dual-pipeline]
related: [[voice-music-common-pitfalls]] [[anon-browser-cdp-death-no-recovery]]
---

# Deepgram "zombie stream" — a user goes silently deaf to Poob with no recovery

> **2026-07-02 regression + real fix:** the recovery mechanism below shipped
> with a flaw that defeated its own purpose — see the addendum at the bottom.
> The original fix wasn't wrong in design, only in one line of it.

## Symptom

`Wake word fired but Deepgram never delivered transcript` logged **18×** in one
audit — **16 of them the same user (`clit_commander`) over a 30-minute window
(01:17–01:46)**, with **no `Deepgram stream disconnected` event** for that user
in the window. Effect: every "Hey Poob …" from that user fired the wake word
and then vanished — Poob never responded — for the whole session, while other
users in the same channel worked fine. Matches the operator's "Poob FAILED in
VC" reports.

## Root cause

Per-user Deepgram WebSockets (`DeepgramStreamManager` in
[dual_pipeline.py](../../src/poob/voice/dual_pipeline.py)) can enter a **zombie**
state: the socket reports `connected=True` and the listener task is alive, but
Deepgram silently stops delivering `Results` — a half-open socket / server-side
stall. The keepalive loop keeps succeeding (KeepAlive sends don't fail on a
half-open socket), so `connected` stays `True` and the listener just blocks on
`async for msg in ws` receiving nothing.

The existing recovery (`send_audio` → throttled reconnect) only triggers on
`connected is False`. A zombie is `connected=True`, so it **never self-heals**.
The local OpenWakeWord detector still fires (it's offline), so the wake is
detected, `_deferred_emit` waits 1.5 s for a transcript that never comes, logs
the miss, and drops the utterance — repeatedly, forever.

## Fix

Detect the zombie by its exact symptom — **wake fired but no transcript** — and
force-rebuild the stream:

- `DeepgramStreamManager._consecutive_lost[user_id]` counts consecutive misses.
- `_listen_loop` resets it to 0 the instant **any** transcript arrives (the
  authoritative liveness signal), and `note_transcript_delivered()` does the
  same on the `_deferred_emit` success path.
- `_deferred_emit`, on its 1.5 s timeout, calls `report_lost_transcript(user)`;
  after `_ZOMBIE_LOST_THRESHOLD = 2` consecutive misses it calls
  `force_reconnect(user)`.
- `force_reconnect()` tears the stream down (`close_user` cancels the listener +
  closes the ws + drops it from `_streams`) and clears the reconnect throttle
  (`_last_connect_time`), so the **next inbound audio frame rebuilds it fresh** —
  the pending-audio ring buffer preserves the recent frames, so the new
  connection comes up with the current speech.

Net: a 30-minute dead spell becomes a ~1 s self-heal on the 2nd missed wake.
Wake-driven detection is deliberate — the zombie only *matters* when the user
is addressing Poob, so this catches exactly the harmful case without
reconnecting healthy-but-idle streams. A spurious trigger (e.g. two
back-to-back wake false-fires with no speech) costs only a cheap reconnect.

Mirrors the [[anon-browser-cdp-death-no-recovery]] pattern: a "connected but
dead" resource that needs active liveness-detection + forced rebuild because
the passive disconnect path never fires.

## Validation

- `tests/unit/test_dual_pipeline.py`: `test_report_lost_transcript_recovers_at_threshold`,
  `test_transcript_delivered_resets_miss_counter`, `test_misses_are_per_user_isolated`,
  `test_force_reconnect_closes_clears_throttle_and_counter`.
- Full unit suite green.
- Post-deploy: `Deepgram stream force-reconnect (zombie recovery)` should appear
  ~once when a stream zombifies (followed by a normal `Deepgram stream
  connected`), and long runs of `never delivered transcript` for one user should
  stop.

## Follow-up

A passive keepalive-side zombie detector (connected + recent audio + stale
last-transcript) was considered but not added — it risks false reconnects
during legitimate silence and the wake-driven trigger already covers every
user-facing case. Revisit only if zombies are observed harming non-wake flows.

## 2026-07-02 addendum — the recovery counter reset defeated the recovery

### Symptom (recurrence)

Same exact pattern reappeared: `Wake word fired but Deepgram never delivered
transcript` **18×** in a 5-hour window, **11 of them the same user**
(`the_._gamer`), only **1** `zombie_recovery=True` (auto-recovery) all night.
One of the misses happened on the very next wake attempt *right after* a
successful response from that same user — proof the stream was chronically,
not transiently, degraded.

### Root cause

The fix above reset `_consecutive_lost[user_id] = 0` on **any** transcript
arriving through `_listen_loop` (comment: *"any transcript = the stream is
alive"*) — not just on a wake-fired success. That assumption was wrong: a
stream can reliably deliver ordinary **passive** background transcripts while
specifically, repeatedly failing to deliver the transcript for a **wake-fired**
utterance within the 1.5 s `_deferred_emit` window. Since passive speech is
far more frequent than wake attempts, an unrelated passive transcript almost
always arrived between two wake-fired misses and reset the counter to 0 before
it could reach `_ZOMBIE_LOST_THRESHOLD = 2` — so the recovery this incident
built almost never fired for exactly the chronically-degraded-but-not-fully-dead
streams it was designed to catch.

### Fix

Removed the "any transcript resets the counter" line from `_listen_loop`. The
**only** valid signal that a user's wake path is healthy is a wake-fired
**success** — `note_transcript_delivered()`, called from `_deferred_emit`'s
success branch (unchanged) — or an explicit `force_reconnect()`. Background
transcript content no longer touches `_consecutive_lost`.

No other behavior changes: `_ZOMBIE_LOST_THRESHOLD` stays at 2 (now that misses
aren't spuriously cleared, two genuinely-consecutive wake-fired misses is a
tight, real signal, not a hair-trigger).

### Validation

`tests/unit/test_dual_pipeline.py::test_listen_loop_does_not_reset_miss_counter_on_unrelated_content`
drives the **real** `_listen_loop` with a fake Deepgram message and asserts the
counter is untouched by unrelated content — this test would have caught the
original regression (the prior test suite only exercised
`note_transcript_delivered`/`report_lost_transcript` directly, never the
listener's own reset).

### Lesson

When a recovery mechanism has TWO paths that write to the same state (here:
the listener's blanket "any content" reset vs. the intentional
success-only reset), the more generous path silently wins and the narrow,
correct one becomes dead code that never gets to do its job. Grep for every
writer of shared recovery state, not just the one you're adding.
