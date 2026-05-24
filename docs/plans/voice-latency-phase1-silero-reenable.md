---
type: plan
status: active
date: 2026-05-24
tags: [voice, latency, vad, silero, refactor]
related: [[voice-latency-optimization]] [[voice-latency-phase2-filler-dispatch]] [[voice-latency-phase3-kokoro]] [[voice-architecture]]
---

# Voice-latency Phase 1 re-enable — shared Silero VAD model + lazy init

## Background

[[voice-latency-optimization]] §Phase 1 declared Silero VAD the "highest impact" item — replacing energy-RMS end-of-speech detection with neural speech probability eliminates the 5-15 s utterance-extension failure mode that's the dominant contributor to voice-pipeline latency. The infrastructure landed (`src/poob/voice/silero_vad.py` has `SileroVADProcessor`, `SpeechDetector`, `SpeechState`, `SileroVADConfig`) and was wired into `VoiceSession`. Then it was disabled, with the disable site at [src/poob/voice/session.py:226-231](../../src/poob/voice/session.py) documenting why:

```python
# Silero VAD disabled — creates per-user model instances that are too slow
# to initialize in multi-user channels. Energy-based VAD with packet gap
# detection is reliable. Silero can be re-enabled once we solve:
# 1. Shared model instance with per-user state management
# 2. Lazy initialization (not on every new user join)
self._silero_vad = None
self._use_silero = False
```

So the work is: solve those two problems, then flip `_use_silero = True`.

## Problem scope

`SileroVADProcessor.__init__` does:
1. `load_silero_vad(onnx=True)` — fetches/loads the ONNX model into memory (~1-2 s wall-clock on first call, cached on subsequent module loads via `silero-vad`'s internal caching).
2. ONNX warmup with a dummy zero-tensor inference call.

If `VoiceSession` creates one `SileroVADProcessor` *per user that joins*, every newly-joined user pays the load+warmup cost on its first frame. In a 5-person voice channel where everyone joins inside a few seconds, that's 5-10 s of repeated work — Discord's audio pipeline misses frames, the bot feels stuck on connect.

The per-user model is also redundant: the model has no per-user training. The only per-user thing is the *state* (ring buffer for 20ms→32ms chunk-size bridging + Silero's internal LSTM state).

## Design — shared model, per-user state

One `SileroVADProcessor` instance lives on the `VoiceSession` (already the shape the disabled-state code assumed). The model loads once and is reused across users via a single lock-protected inference path. Per-user state lives in a separate `_PerUserSileroState` dataclass:

```python
@dataclass
class _PerUserSileroState:
    buffer: collections.deque[float]  # 20ms→32ms chunk bridging
    # Silero LSTM state captured between calls so user-switches
    # don't bleed acoustic context across speakers.
    saved_state: dict | None = None  # serialized model state from last call
```

The processor exposes `process_frame(user_id, pcm_bytes) -> list[float]` that:
1. Looks up (or lazily creates) the per-user state dict.
2. Acquires the model lock.
3. Restores Silero's internal state from `saved_state` (if present).
4. Runs the inference for the chunk(s).
5. Saves the post-inference state back into `saved_state`.
6. Releases the lock.
7. Returns probabilities.

The "save and restore" is the cost of sharing the model across users with overlapping speech. Silero's ONNX export exposes the hidden state via inputs/outputs (`h` and `c` tensors). If state-save/restore turns out to be expensive, the fallback is to reset state per-user-switch and accept that overlapping concurrent speech loses a frame or two of context at the user-boundary (in practice users don't talk simultaneously enough for this to matter).

## Lazy init

`VoiceSession.__init__` no longer constructs `SileroVADProcessor()`. Instead it stores `None` and constructs lazily on the first frame after speech is detected by the cheap energy gate. This keeps the load+warmup off the connect-time critical path; the cost amortizes into the first utterance instead of forming a join-time stall.

For a multi-user channel: the first user to speak pays the load cost (~1-2 s); subsequent users pay nothing. The total is bounded by the model-load cost regardless of how many users join.

## Open question — Silero ONNX state save/restore feasibility

I haven't validated whether the `silero-vad` PyPI package's ONNX path actually exposes the LSTM hidden state for save/restore on the public `model(tensor, sample_rate)` API. There are three possible answers:

1. **State save/restore IS supported** — design above works as-is.
2. **NOT supported on the ONNX path** — fall back to per-user-switch reset, accept some accuracy loss on overlapping speech.
3. **Not supported AND the reset cost is high** — keep per-user model instances but make the load lazy (only on first speech from that user) so single-speaker channels pay zero idle cost. Multi-user channels still pay per-user load but only when a user actually speaks for the first time.

The first ~30 min of implementation will probe the package's API surface and pick the right path. Decision lands in the follow-up decision note.

## Test plan (TDD)

`tests/unit/test_silero_vad_shared.py` (new file):

1. `test_processor_loads_once_across_users` — two `process_frame(user_id=A, ...)` and `process_frame(user_id=B, ...)` calls hit the same underlying model instance (assert a probe counter on a mock).
2. `test_per_user_buffer_isolation` — A's partial-chunk audio doesn't bleed into B's probabilities.
3. `test_user_state_persists_across_frames` — A's second frame sees state from A's first frame (probe via a mock or a deterministic synthetic-audio test).
4. `test_lazy_init_skips_load_when_no_speech` — `VoiceSession` constructed; no `process_frame` called; no model load happened. Probe via mock or by patching `load_silero_vad` to raise.
5. `test_silero_speech_probability_overrides_energy_when_enabled` — when `_use_silero=True`, end-of-speech detection uses Silero's probability state, not the RMS gate. Integration-ish but mockable.

Plus run the full unit suite and verify the existing energy-VAD path still works when `_use_silero=False` (the fallback stays in for ops who don't want Silero on by default).

## Files to change

- [src/poob/voice/silero_vad.py](../../src/poob/voice/silero_vad.py) — `SileroVADProcessor` gains `(user_id, ...)` overload + per-user state dict. Lock around inference. Investigate ONNX state save/restore.
- [src/poob/voice/session.py](../../src/poob/voice/session.py) — lazy construct + flip `_use_silero` gating to use the shared instance. Keep the energy-VAD path as a fallback gated by config.
- [src/poob/config.py](../../src/poob/config.py) — new `voice_use_silero_vad: bool = False` (opt-in v1; default off so behavior change is gated and rollback is one env var).
- [tests/unit/test_silero_vad_shared.py](../../tests/unit/test_silero_vad_shared.py) — NEW, 5 tests.
- Possibly [tests/unit/test_silero_vad.py](../../tests/unit/test_silero_vad.py) if it exists for the per-user processor — update for the new contract.

## Ship strategy

Two-commit ship:
- **Commit A**: refactor + lazy + per-user state + tests. Default `voice_use_silero_vad=False`. Suite must stay green.
- **Commit B**: after operator verifies the opt-in path works in a real voice channel (sets the env var, listens, confirms no breakage), flip the default to `True`. Single line + decision note.

This way the opt-in flag gives us reversibility for free; if the new architecture turns out to have a different latency or accuracy regression, the operator clears the env var and rolls back to the energy gate without code change.

## Acceptance

- ✅ 5 new tests pass; full suite stays at 1202.
- ✅ Manual: in a real multi-user voice channel, with `VOICE_USE_SILERO_VAD=true` set, end-of-speech detection feels snappier (no more 5-15 s utterance extensions on light/intermittent breathing).
- ✅ Manual: connect-time stall when 5 users join in quick succession is eliminated (only the first speaker triggers the model load; the others reuse it).
- ✅ Decision note documents the chosen state-save/restore strategy (1, 2, or 3 above).
- ✅ This plan archives to "Superseded / historical" once Commit A lands.

## Deferred

- **Promote `voice_use_silero_vad=True` as default.** Commit B above; gated on real-channel validation.
- **Tune `SileroVADConfig` thresholds** (speech probability cutoff, hysteresis windows). The 0.5 default is reasonable; tuning is a knob, not a feature.
- **Adaptive resource budget** (drop to energy VAD if CPU is overloaded). Premature; revisit if anyone reports CPU pressure.
- **Move VAD to a dedicated subprocess** so it can run on a different core without GIL contention. Premature; current inference is <1 ms per frame.
