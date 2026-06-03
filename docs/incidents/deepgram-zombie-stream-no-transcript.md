---
type: incident
status: resolved
date: 2026-06-03
tags: [voice, deepgram, stt, reliability, dual-pipeline]
related: [[voice-music-common-pitfalls]] [[anon-browser-cdp-death-no-recovery]]
---

# Deepgram "zombie stream" — a user goes silently deaf to Poob with no recovery

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
