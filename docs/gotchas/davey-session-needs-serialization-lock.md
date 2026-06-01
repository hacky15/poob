---
type: gotcha
status: active
date: 2026-06-01
tags: [voice, dave, davey, voice-compat, ffi, threading, reconnect]
related: [[voice-4014-reconnect-event-loop-wedge]] [[dave-ready-flag-is-not-truth]] [[project_pycord_migration]]
---

# A shared `davey.DaveSession` MUST be serialized with a lock — the Pycord migration dropped it

## Trigger

Touching `src/poob/voice/voice_compat.py` — anything that calls a `davey` (DAVE E2EE, Rust/FFI) method: `decrypt`, `encrypt_opus`, `reinit`, `DaveSession(...)`, `get_serialized_key_package`, `process_proposals`, `process_commit`, `process_welcome`, `reset`, `set_passthrough_mode`.

## The hazard

A single `DaveSession` object is touched by **three threads**:
- the **recv thread** → `decrypt(...)` per inbound audio frame
- the **AudioPlayer thread** → `encrypt_opus(...)` per outbound frame
- the **event-loop thread** → MLS re-keying during voice handshake / 4014 reconnect (`reinit`, `process_commit/welcome`, `get_serialized_key_package`)

`davey` is a compiled C-extension; every method is a synchronous native call with **no internal locking exposed to us**. Concurrent access to the MLS ratcheting tree across the FFI boundary causes **Rust panics or a native deadlock**. A native call that deadlocks while holding the GIL **pins the entire asyncio event-loop thread** — which freezes voice, the text gateway, AND the patrol engine simultaneously (they share one loop), with zero log output at idle CPU. Recovery requires a process restart. See [[voice-4014-reconnect-event-loop-wedge]] for the 8-minute production outage this caused.

## Why this is easy to reintroduce

The **deprecated** `dave_patch.py:33-37` (discord.py era) had this exactly right:

```python
# Global lock serializing all access to dave_session (encrypt + decrypt).
# ... without a lock, concurrent access to the MLS ratcheting tree causes
# Rust panics across the FFI boundary.
dave_lock = threading.Lock()
```

The **active** Pycord patch (`voice_compat.py`) **dropped that lock** during the migration ([[project_pycord_migration]]) — grep `voice_compat.py` for `Lock|acquire|threadsafe` returns nothing. There was no decision/gotcha recording that the lock was safe to drop; it simply went missing. It is NOT safe to drop.

## Do

- Guard **every** `davey.DaveSession` call with a session-scoped `threading.Lock` (not `asyncio.Lock` — decrypt/encrypt run on real threads, not the loop). Mirror the deprecated `dave_patch.py` model.
- Run the MLS re-keying native calls (reinit / DaveSession / get_serialized_key_package) **off the event loop** via `loop.run_in_executor(...)`, so a native stall can't pin the loop thread.
- On a 4014 force-disconnect, **reset stale MLS state** (`dave_pending_transitions`) before reinit.

## Don't

- Don't call any `davey` method directly on the event loop without the lock + executor offload.
- Don't try to fix the wedge with `asyncio.wait_for` — it **cannot cancel a wedged native call** (same lesson as [[main-browser-cdp-wedge-infinite-hang]]).
- Don't remove the lock again "because it looks unused" — it's load-bearing exactly during the rare reconnect window.

## Reference

[[voice-4014-reconnect-event-loop-wedge]] (the incident + full fix plan), `dave_patch.py:33-37` (the documented invariant).
