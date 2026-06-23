---
type: incident
status: resolved
date: 2026-06-22
tags: [voice, sink, deepgram, crash, logging, regression]
related: [[deepgram-streams-leak-on-cleanup]] [[voice-architecture]] [[openwakeword-lazy-init-not-thread-safe]]
---

# Stale-check on a torn-down pipeline flooded 59k tracebacks and crippled the bot

## Symptom

A 5-hour voice window logged **478,355 lines including 59,247 tracebacks** (~10/sec,
~593/min, sustained for 11+ minutes), while poob produced only **2 responses** and
repeatedly logged "Wake word fired but Deepgram never delivered transcript." Poob
was effectively non-functional ("not correctly behaving").

Every traceback was identical:

```
File "src/poob/voice/realtime_sink.py", line 99, in _check_stale_buffers
    self._on_stale_check()
File "src/poob/discord_bot/cogs/voice_cog.py", line 275, in <lambda>
    on_stale_check=lambda: session._dual_pipeline.check_stale_buffers()
AttributeError: 'NoneType' object has no attribute 'check_stale_buffers'
```

## Root cause (a regression)

`RealtimeAudioSink` runs a background thread that calls `on_stale_check` **every
100 ms** (10x/sec) to flush buffers when Discord stops sending packets.

The Deepgram-leak fix ([[deepgram-streams-leak-on-cleanup]]) added
`self._dual_pipeline = None` to `VoiceSession.cleanup()`. But the sink's stale-check
lambda dereferenced `session._dual_pipeline` **with no None guard**. When a session
cleaned up (or a reconnect orphaned a sink whose recording wasn't stopped) while the
sink kept running, the lambda hit `None` → `AttributeError`, caught by
`_check_stale_buffers` and logged via `logger.exception` (full traceback) **on every
100 ms tick**.

The traceback storm (string-formatting + log I/O 10x/sec under the GIL) starved the
audio recording/STT threads — which is why wake fired but Deepgram "never delivered"
and only 2 requests got answered.

## Fix

1. **None-safe callback** ([voice_cog.py](../../src/poob/discord_bot/cogs/voice_cog.py)):
   the stale-check no-ops when `session._dual_pipeline is None` instead of
   dereferencing it. A stale-check on a torn-down pipeline is legitimately a no-op.
2. **Throttled error logging** ([realtime_sink.py](../../src/poob/voice/realtime_sink.py)):
   `_log_throttled` logs any recurring stale-check error **at most once per 30 s**, so
   no future recurring error in this 10x/sec loop can ever flood again (defense for
   the whole class, not just this instance).

## Validation

`tests/unit/test_realtime_sink_stale.py`: 200 identical failures collapse to 1 log
line (throttled); a None-safe callback logs nothing.

## Lesson

A caught-and-logged exception inside a high-frequency loop (10x/sec) is a latent
log-flood / thread-starvation bomb. Any callback invoked from such a loop must (a)
tolerate torn-down state without raising, and (b) have its error path rate-limited.
Setting shared state to `None` in cleanup requires auditing every callback that
captures it.
