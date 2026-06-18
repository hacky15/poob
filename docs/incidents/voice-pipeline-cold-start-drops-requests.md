---
type: incident
status: resolved
date: 2026-06-17
tags: [voice, wake-word, stt, latency, cold-start, observability]
related: [[voice-architecture]] [[wake-word-dual-gate]] [[wake-word-v3-shipped]] [[zombie-voice-connection-blocks-autojoin]]
---

# Voice pipeline cold-start: models load lazily AFTER join → first requests dropped

## Symptom

Operator (2026-06-17): "simple requests across the board but not handling my
voice requests — and when it finally did, it had great delay." Recurring across
sessions.

## Root cause (proven by load timestamps)

Every voice model initialized **lazily, on first use, AFTER the bot joined** —
not at join. From a fresh session (guild 1483…, join 22:26:30):

```
22:26:51  Deepgram stream connected        (+21s)
22:26:54  OpenWakeWord loaded              (+24s)
22:27:20  Model2Vec loaded for scoring     (+50s)
```

So for ~25–50s after Poob joined, the wake / STT / address pipeline wasn't
ready. Requests in that window were **silently dropped** — no transcript, no
log, nothing to even audit. The first captured utterance was at +38s. That cold
window IS the "not handling my requests + great delay": the user spoke, nothing
happened, they repeated until the pipeline finally warmed up.

`WakeWordDetector._ensure_model` and `MultiSignalAddressDetector._ensure_model`
were deliberately lazy ("deferred to the first speech frame so multi-user joins
don't stall") — but that pushed the load cost onto the *first speaker* instead
of off the critical path.

### Secondary: wake-misses were undiagnosable

Even once warm, some attempts logged `wake_word=False` with no indication *why*
(`'Loops up.'`, `'It proves up.'`). The OWW score was computed per frame
(`s` in `process_frame`) and **discarded**; neither it nor the text/audio gate
signals were logged. We were blind to whether the wake model missed, STT
misheard, or the user didn't address the bot — which is why wake-reliability
kept recurring without resolution.

## Fix

1. **Background prewarm on join.** `VoiceSession.__init__` schedules
   `_prewarm_models()`, which loads OpenWakeWord (`DualPipelineProcessor.prewarm`
   → `WakeWordDetector.prewarm`) and Model2Vec (`MultiSignalAddressDetector.
   prewarm`) in a thread executor — non-blocking (preserves the
   no-join-stall reason the loads were deferred), but ready in ~seconds rather
   than on first speech. The cold window collapses from ~25–50s to ~the model
   load time, off the critical path.
2. **Wake-gate instrumentation.** `WakeWordDetector` tracks the per-utterance
   peak OWW score; `_emit_utterance_locked` logs a `Wake gate decision` event
   with `text_match`, `audio_match`, `oww_peak`, and `threshold`. A wake-miss
   now reads e.g. `addressed=False text_match=False audio_match=False
   oww_peak=0.41 threshold=0.7` — actionable for tuning the threshold or
   confirming an STT mishear, instead of a blind `wake_word=False`.

Files: [voice/session.py](../../src/poob/voice/session.py),
[voice/dual_pipeline.py](../../src/poob/voice/dual_pipeline.py),
[voice/address_detector.py](../../src/poob/voice/address_detector.py).

## Validation

`tests/unit/test_voice_prewarm.py` — prewarm loads both models (mocked),
session warms both in the executor, no-dual-pipeline path is safe, peak-score
tracks + resets, and grep-as-tests lock the join-prewarm trigger + the gate
instrumentation. Full unit suite green.

## Follow-up (now diagnosable, not guessed)

The warm-window wake-misses (`wake_word=False` while OWW was loaded) point at
the [[wake-word-v3-shipped]] generalization gap. With the new `Wake gate
decision` log, the next session can read the actual `oww_peak` distribution on
real misses and decide between a threshold tweak (load-bearing per
[[wake-word-dual-gate]] — cite before changing) and the queued v4 retrain.
Don't guess; read the peaks.
