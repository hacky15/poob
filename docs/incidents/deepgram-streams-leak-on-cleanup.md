---
type: incident
status: resolved
date: 2026-06-15
tags: [voice, deepgram, stt, cleanup, resource-leak]
related: [[voice-architecture]] [[deepgram-zombie-stream-no-transcript]] [[persistent-voice-log]]
---

# Deepgram WebSockets (and the keepalive task) leaked on voice-session teardown

## Symptom

Per-user Deepgram STT WebSockets stayed open **~an hour after the voice session
ended**. Surfaced by the 2026-06-15 forensic audit (text-surface, finding 1).

Evidence (poob_session_full.log):

```
03:04:54.892  All users left voice, auto-disconnecting   guild=770491927580639295
03:04:54.893  Voice session cleaned up
03:04:55.028  GuildMusicPlayer destroyed
   ... ~1 hour later ...
04:04:51.877  Deepgram stream disconnected user=726448122842316924
04:04:53.xxx  Deepgram stream disconnected user=478697650556895251
04:04:56.xxx  Deepgram stream disconnected user=770471493079007252
```

The streams only died an hour later — on their own (idle/server-side), not
because we closed them. Across many sessions this accumulates zombie sockets.

## Root cause

Two gaps in the teardown chain:

1. **`VoiceSession.cleanup()` never called `self._dual_pipeline.cleanup()`.** It
   cancelled inflight response tasks, flushed `_user_buffers` /
   `_speech_detectors`, and unlinked the music player — but the dual pipeline
   (which owns the `DeepgramStreamManager` and every per-user WebSocket) was
   never torn down. The streams were simply abandoned.
2. **`DeepgramStreamManager.cleanup()` closed the streams but never cancelled
   its own keepalive task** (`_keepalive_task`, a `while True` loop started by
   `start_keepalive_loop`). So even when cleanup *was* reached, it leaked a
   live background task that kept iterating.

## Fix

Close the whole chain:

- `VoiceSession.cleanup()` now `await self._dual_pipeline.cleanup()` (guarded
  for the energy-VAD-only path where it is `None`) and drops the reference.
  [voice/session.py](../../src/poob/voice/session.py)
- `DeepgramStreamManager.cleanup()` now cancels `_keepalive_task` (and nulls it)
  before closing each stream via `close_user` (which sends `CloseStream`,
  closes the WS, and cancels the listener task).
  [voice/dual_pipeline.py](../../src/poob/voice/dual_pipeline.py)

`DualPipelineProcessor.cleanup()` already chained `_wake_detector.cleanup()` +
`_deepgram.cleanup()`, so wiring the session call completes the path.

## Validation

`tests/unit/test_deepgram_cleanup.py`:
- `test_deepgram_cleanup_cancels_keepalive_and_closes_streams` — keepalive task
  cancelled + `close_user` called for every stream.
- `test_voice_session_cleanup_tears_down_dual_pipeline` — `_dual_pipeline.cleanup()`
  awaited and reference dropped.
- defensive no-keepalive / no-pipeline paths don't crash; grep-as-test locks
  both halves of the chain.
- Full unit suite green.

## Related

Distinct from [[deepgram-zombie-stream-no-transcript]] — that is a *mid-session*
half-open stream healed by force-reconnect; this is an *end-of-session* leak of
otherwise-healthy streams.
