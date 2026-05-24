---
type: decision
status: active
date: 2026-05-24
tags: [voice, latency, vad, silero, refactor]
supersedes: []
related: [[voice-latency-phase1-silero-reenable]] [[voice-latency-optimization]] [[voice-architecture]] [[voice-latency-phase2-filler-dispatch]] [[voice-latency-phase3-kokoro]]
---

# Voice-latency Phase 1 — Silero VAD shared-model refactor (re-enabled, opt-in)

## Context

Phase 1 of [[voice-latency-optimization]] declared neural Silero VAD the "highest impact" item — energy-RMS end-of-speech detection has a long failure tail (5-15 s utterance extension on quiet trailing speech), and a real speech-probability gate eliminates it. The `SileroVADProcessor` + `SpeechDetector` infrastructure landed earlier but was disabled at the `VoiceSession` integration point. The disable comment at `voice/session.py` named two blockers:

1. **Per-user model load.** `VoiceSession.get_or_create_buffer` constructed a fresh `SileroVADProcessor` per Discord user, and the constructor ran `load_silero_vad(onnx=True)` + an ONNX warmup inference — ~1-2 s per user. In a 5-user channel everyone joining within a few seconds, this stacked into 5-10 s of repeated work that starved Discord's audio pipeline.
2. **Init on every join.** The processor was instantiated lazily but inside the synchronous join path, so the latency hit landed on the connect moment instead of being absorbed by background work.

[[voice-latency-phase1-silero-reenable]] worked the design end-to-end: shared `SileroVADProcessor` (one per session, not per user), per-user state isolation, lazy model load deferred to first inference frame. That plan was the precondition for this commit.

## Decision

Ship the shared-model + lazy-init refactor. Re-enable Silero VAD as an **operator-opt-in** via the `VOICE_USE_SILERO_VAD` environment variable, defaulting to `False`. The energy-RMS gate remains the active path on rollout; flipping the default to `True` happens in a follow-up commit after a live voice-channel A/B confirms the refactor is correct in production.

Concrete changes:

- **`SileroVADProcessor`** (src/poob/voice/silero_vad.py)
  - Constructor no longer loads the model. `self._model` is `None` until the first inference call.
  - New `process_frame_for_user(user_id, pcm_bytes)` entry point. Maintains a `_PerUserSileroState` dict (per-user 320-sample ring buffer + cloned LSTM `_state` / `_context` snapshot). On every inference: restore the user's saved tensors onto `self._model._state` / `._context`, call the model, snapshot the new state back into the dict.
  - State save/restore uses `torch.Tensor.clone()` — verified against the real `silero-vad` ONNX wrapper (`_state` shape `[2, 1, 128]`, `_context` shape `[1, 64]`).
  - Backwards-compatible `process_frame(pcm_bytes)` shim routes through `process_frame_for_user(0, ...)` so any legacy caller stays alive.
- **`SpeechDetector`** (same file) now passes its own `user_id` to `process_frame_for_user`, so per-user LSTM state stays isolated even though the underlying model is shared.
- **`VoiceSession`** (src/poob/voice/session.py)
  - New `use_silero_vad: bool = False` constructor kwarg.
  - `get_or_create_buffer` lazy-constructs **one** `SileroVADProcessor` on the session the first time a buffer is requested and the flag is on. Every per-user `SpeechDetector` receives that shared processor via the `vad_processor=` kwarg (which existed all along but was never wired — that was the real bug).
- **`AppConfig`** (src/poob/config.py) — new `voice_use_silero_vad: bool = False` field bound to `VOICE_USE_SILERO_VAD`.
- **`main.py`** voice-session factory passes `use_silero_vad=config.voice_use_silero_vad` through.

## Alternatives considered

- **Process-pool out-of-band inference.** Push Silero to a worker process so model load doesn't block the join. Rejected: adds IPC latency to every 32 ms inference (we have ~10-15 ms total budget for VAD before it bleeds into TTS scheduling), and the shared-model fix already removes the load cost from the hot path. The cost was paying for load N times, not paying for load at all.
- **Eager load at session start.** Construct the processor when the bot joins voice, before any user speaks, so the load cost lands on join, not first speech. Rejected: connect path is already latency-sensitive (Discord's voice-WS handshake), and the lazy path handles the load *off* the connect frame entirely — by the time a user actually speaks, the load (~1-2 s) is done in time for the inference (which only fires once 512 samples of resampled audio have accumulated, ~32 ms). Lazy is strictly better on multi-user joins where some users never speak.
- **Skip per-user state, share global state.** Cheaper, but the Silero LSTM hidden state is acoustic context — without per-user isolation, user B's silence frames would corrupt user A's mid-sentence probability stream and vice versa. Empirically degrades end-of-speech detection on overlapping speech (the exact failure mode the refactor was supposed to fix).
- **Flip default `True` in this same commit.** Rejected per operator standing rule ("two-commit ship strategy" for behavior changes that move user-perceptible audio paths) — keeps rollout reversible by env flag, gives a clean A/B against the energy gate before the default flip.

## Consequences

- **No behavior change on rollout.** `VOICE_USE_SILERO_VAD` defaults False; the energy-RMS path stays active. Operator must explicitly opt in to A/B.
- **Multi-user join stall fixed.** One model load per session, deferred until first speech frame. A 5-user channel joining inside a few seconds pays load cost once, off the connect frame.
- **Per-user state isolation preserved.** The save/restore dance around `model._state` / `model._context` keeps user A's acoustic context separate from user B's; verified by `tests/unit/test_silero_vad_shared.py::TestPerUserStateIsolation`.
- **Locking — single-threaded today, will need a lock if that changes.** Discord audio frames arrive sequentially per `VoiceSession`, so save/restore around the model call is currently safe without a lock. If future work moves frame dispatch off the single voice thread, wrap the inference + save/restore in an `asyncio.Lock` (or `threading.Lock` if it's a thread-pool). Documented inline at the top of `SileroVADProcessor`.
- **Next step (operator):** validate in a real voice channel by setting `VOICE_USE_SILERO_VAD=true` in `.env`, running for a session, and comparing end-of-speech latency against the energy-gate baseline. If the refactor holds up, the follow-up commit flips the field default to `True`.
- **Plan archive.** [[voice-latency-phase1-silero-reenable]] moves to Superseded in [[plans/README]].

## Validation

- New unit tests (5 in `tests/unit/test_silero_vad_shared.py`):
  - constructor does NOT load the model
  - first frame loads model exactly once across all users + all frames
  - per-user buffers don't bleed (A's 640-sample crossing doesn't pull B's chunk forward)
  - per-user LSTM state is restored correctly when A speaks → B speaks → A speaks
  - `voice_use_silero_vad` defaults False
- Full unit suite: **1207 passed, 1 skipped** (1202 prior baseline + 5 new tests).
