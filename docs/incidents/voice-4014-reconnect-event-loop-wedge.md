---
type: incident
status: resolved
date: 2026-06-01
tags: [voice, dave, voice-compat, davey, event-loop, reconnect, pycord]
related: [[dave-ready-flag-is-not-truth]] [[dave-timeout-fail-hard-regression]] [[dave-handshake-failure-april2026]] [[davey-session-needs-serialization-lock]] [[main-browser-cdp-wedge-infinite-hang]] [[project_pycord_migration]] [[gateway-keepalive-result-block-no-recovery]]
---

# Voice WS 4014 reconnect wedged the entire event loop (voice + text dead)

## Symptom

During an active multi-user voice session (2026-06-01 ~02:02 UTC), the bot went **completely unresponsive for 8 minutes** — voice AND text chat both dead, zero log output, process alive at ~1.7% CPU. Only a `docker restart poob` recovered it. The owner reported: voice "join" worked earlier, then nothing responded in voice or chat.

Exact log sequence:
```
02:02:09.508  Wake word detected (omosessuale)
02:02:09.590  Wake word detected (I FART BLOOD)
WARNING [poob.voice.voice_compat] Voice websocket closed: code=4014 reason=Disconnected. type=8
INFO    [discord.voice_client] Disconnected from voice by force... potentially reconnecting.
INFO    [discord.voice_client] Voice handshake complete. Endpoint found c-ord10-...discord.media
02:02:10.97  (a few trailing passive transcripts)
02:02:25     patrol cycle finished → "Waiting for next patrol, next_at 02:18:09"
             ───── TOTAL SILENCE 02:02:25 → 02:10 (restart) ─────
```

WS **4014** is a Discord **voice** websocket close ("Disconnected" — bot moved/disconnected or session invalidated). The mystery: a voice-only socket close also killed **text** chat (separate gateway). That only happens if the shared asyncio event-loop thread itself is blocked.

## Root cause

The 4014 triggers Pycord's internal voice reconnect (`voice_client.poll_voice_ws` → `potential_reconnect` → `connect_websocket`), which re-runs the DAVE MLS handshake. On `SESSION_DESCRIPTION` ([voice_compat.py:360-367](../../src/poob/voice/voice_compat.py)) it calls `state.reinit_dave_session()` → synchronous **native `davey` (Rust/FFI)** calls **on the event-loop thread**: `dave_session.reinit(...)` / `davey.DaveSession(...)` ([:150/:152](../../src/poob/voice/voice_compat.py)) and `get_serialized_key_package()` ([:167]).

These run **with no lock**, while two other threads touch the *same* `DaveSession`:
- recv thread → `session.decrypt(...)` per inbound frame ([:471])
- AudioPlayer thread → `session.encrypt_opus(...)` ([:576])

The **deprecated predecessor patch documents this exact hazard as requiring a lock**: [dave_patch.py:33-37](../../src/poob/voice/dave_patch.py#L33) — *"Global lock serializing all access to dave_session… without a lock, concurrent access to the MLS ratcheting tree causes Rust panics across the FFI boundary."* The Pycord migration ([[project_pycord_migration]]) dropped that lock and **no vault note recorded that it was safe to drop** — that omission is itself a finding.

A 4014 reconnect during active speech is precisely the window where the loop thread rebuilds the MLS tree while recv/player threads call decrypt/encrypt on the same object. A native FFI call that deadlocks on an internal mutex (or panics with the GIL held) **pins the single event-loop thread** — freezing voice, the text gateway, and patrol together, with zero log output at idle CPU. Exactly the observed signature. This is the same wedge *class* as [[main-browser-cdp-wedge-infinite-hang]] (2026-05-31): `asyncio.wait_for` cannot cancel a wedged native call.

Contributing trigger: two wake words fired ~80 ms apart immediately before the 4014, so a multi-user MLS epoch transition was in flight (`dave_pending_transitions` not cleared across the force-disconnect — [[dave-handshake-failure-april2026]] Mechanism 3) when the rebuild ran.

## My recent deploys are NOT the cause (accountability)

Verified decisively. The recent Deepgram-STT reconnect frame-buffering (`_buffer_pending`/`_flush_pending`), utterance dedup (`_is_duplicate_emit`), persona-prompt, and model2vec changes are all on the **Deepgram STT socket** — a separate connection from the Discord voice WS/DAVE layer:
- grep of `dual_pipeline.py` for `voice_compat|davey|dave|voice_client|connect_websocket` → zero hits.
- `_buffer_pending` is a bounded `deque(maxlen=150)`, O(1), no await/lock.
- audio-thread→loop handoffs are fire-and-forget `run_coroutine_threadsafe` with no `.result()` — cannot back-pressure the loop.

Those queued coroutines piled up *unexecuted* on the already-wedged loop (a symptom), but are not on the reconnect path.

## Addendum (2026-09-05) — status correction: items 1-2 were already shipped, undocumented; item 5 now shipped

Investigating a fresh, mechanistically-different gateway freeze
([[gateway-keepalive-result-block-no-recovery]]), this note was the first
hypothesis followed — but a direct grep of the current
`src/poob/voice/voice_compat.py` shows items 1 (`_dave_lock` guarding
every native `davey` call site) and 2 (the expensive re-key native call
wrapped in `loop.run_in_executor`) below are **already fully implemented
and correct**, verified by reading every `_dave_lock` call site directly —
none hold the lock across an `await`. This note's `status: active` was
stale documentation from a fix that shipped (likely alongside the
`davey-session-needs-serialization-lock` gotcha's own authoring, same
date) without this note being updated to match. It is not a live gap.

Item 5 (bot-process loop-liveness watchdog) genuinely was never shipped
until [[gateway-keepalive-result-block-no-recovery]]'s `GatewayWatchdog`.
Items 3-4 (reset stale MLS state on 4014; lock-release on disconnect in
`_play_audio`) were not re-verified in this pass — flagging as unconfirmed
rather than claiming they're done without checking.

Marking this note `resolved` for the parts confirmed shipped; if items 3-4
are later found un-shipped, re-open with a note here rather than silently
fixing without recording the reopening.

## Fix (planned — not yet shipped; see status)

Does NOT touch the protected soft-fails (`dave_session.ready` soft-fail per [[dave-ready-flag-is-not-truth]]; the first-join 15 s "start anyway" guard per [[dave-timeout-fail-hard-regression]]). It re-establishes a *previously-documented-as-required* invariant.

1. **Serialize all `davey.DaveSession` access (root fix).** Re-add a session-scoped `threading.Lock` around every native davey call: `decrypt` ([:471]), `encrypt_opus` ([:576]), `reinit`/`DaveSession`/`get_serialized_key_package` ([:150/:152/:167]), `process_proposals`/`process_commit`/`process_welcome` ([:302/:317/:330]), `reset`/`set_passthrough_mode` ([:170-171]). `threading.Lock` (not asyncio) because decrypt/encrypt run on real threads.
2. **Move reconnect-path native calls off the loop.** Wrap the `reinit_dave_session` native calls in `await loop.run_in_executor(...)` so a native stall can't pin the loop thread (lock still serializes against the audio threads inside the executor job). Do NOT offload the per-frame decrypt/encrypt (latency).
3. **Reset stale MLS state on 4014.** Clear `dave_pending_transitions` + reset the session before reinit so the rebuild starts clean.
4. **Lock-release on disconnect (symptom fix).** Guard `await play_done` in `_play_audio` ([session.py:1255-1272](../../src/poob/voice/session.py)) so a `_connected.clear()` mid-playback can't hold `_response_lock` / stick `_is_speaking=True` forever.
5. **Backstop (separate change):** bot-process loop-liveness watchdog (the patrol scheduler has one; the Discord bot does not) — the only guaranteed recovery if a future native wedge slips past 1-4. Cross-ref [[main-browser-cdp-wedge-infinite-hang]].
6. **Observability:** one INFO at reconnect entry/exit (`DAVE reinit start/done elapsed_ms`) so a future stall is bounded in logs, not silent.

## Validation / how to confirm the intermittent root cause

- **`py-spy dump` on a wedged container before restart** (decisive): main-thread stack inside a `davey` native frame confirms the unlocked-FFI wedge.
- Repro: in voice with 2+ active speakers + DAVE, force-move the bot mid-TTS-playback to induce a 4014 while decrypt/encrypt are in flight. Pre-fix → freeze; post-fix → reconnects without stalling the text gateway.
- Tests (TDD): davey-lock acquisition on every entry point; concurrent decrypt+reinit serialize; reinit offloaded via executor; 4014 clears pending transitions; play_done releases `_response_lock` on disconnect.

## Follow-ups

- Immediate recovery this incident: `docker restart poob` (Bot is ready 02:15:25). Fix not yet deployed — deploy timing TBD with operator (a redeploy interrupts the live voice session).
- Gotcha: [[davey-session-needs-serialization-lock]].
