---
type: plan
status: active
date: 2026-05-23
tags: [voice, latency, tts, kokoro, local-inference]
related: [[voice-latency-optimization]] [[voice-architecture]] [[music-player-architecture]] [[tts-loudness-speechnorm]]
---

# Voice-latency Phase 3 ship — enable Kokoro local TTS

## Goal

Eliminate the 0.8-1.2 s network round-trip on every Poob spoken response by switching to local Kokoro-82M TTS. Time-to-first-audio drops from ~1 s to <300 ms on CPU (and <100 ms on the homelab's GPU if it's available). This is item 3 of the 5-phase plan in [[voice-latency-optimization]]; Phase 1 (Silero VAD) shipped (then was disabled for the per-user-model issue documented in `session.py`), Phase 5 (streaming STT via Deepgram dual-pipeline) is effectively done, Phases 2 and 4 are partial.

## What's already done

Most of the lift is already in the tree from a prior pass:

- [src/poob/voice/tts.py:173](../../src/poob/voice/tts.py) — `KokoroTTS` class fully implemented: lazy-imports `kokoro_onnx`, lazy-constructs the model, runs synthesis in an executor, returns 16-bit WAV bytes the existing pipeline already understands.
- [src/poob/voice/tts.py:236](../../src/poob/voice/tts.py) — `build_tts_cascade` already knows about `kokoro` as a preferred-provider value; gated by `is_available()` which short-circuits to False when `kokoro-onnx` isn't installed.
- [src/poob/config.py:294](../../src/poob/config.py) — `voice_tts_provider: str = "google_tts"` already documents `kokoro` as a valid value in its inline comment.

What's NOT shipped:

1. `kokoro-onnx` is not in `pyproject.toml` — the `is_available()` check always returns False in production.
2. The model + voices files aren't pre-downloaded into the image — on first invocation, `Kokoro()` would block the event loop for the ~350 MB download.
3. There are no unit tests around `KokoroTTS` — the class is unverified.
4. No decision note documents the choice; the voice-latency-optimization plan is the only reference and it's a forward-looking design doc, not a record of the ship.

## Scope

In-scope (ship in one commit):

1. Add `kokoro-onnx>=0.4.0` to `pyproject.toml`.
2. Dockerfile RUN step to pre-download the Kokoro ONNX model + voices file at image-build time (mirroring the `openwakeword.utils.download_models` pattern at Dockerfile line 51). Image gets bigger by ~350 MB; that's the cost.
3. `tests/unit/test_kokoro_tts.py` — mock-based unit tests that verify the class without requiring `kokoro-onnx` installed.
4. Decision note: when to opt into Kokoro, the fallback behavior, why the default stays `google_tts` for now.
5. Update [[voice-latency-optimization]] plan note: mark Phase 3 as shipped, link forward to the new decision note.

Out-of-scope (deferred):

- **Making Kokoro the default provider.** Today's default is Google Cloud TTS (Chirp3-HD-Fenrir voice) which is high-quality cloud audio. Switching the default is a behavior change that needs evidence Kokoro's quality matches in the user's actual voice channels. Opt-in via env var first; flip the default later if/when quality holds up in real use.
- **Phase 2 (filler audio enable).** `FillerPlayer` is instantiated but apparently isn't actively dispatching clips in the response path. Separate ship.
- **Phase 4 (sentence-streaming overlap).** Already partially implemented; gluing it together with Phase 3 is a second commit.
- **espeak-ng system dep.** The `kokoro-onnx` package has a built-in g2p tokenizer; no separate espeak install needed. (The full `kokoro` Python package does need it; we use `kokoro-onnx` specifically to avoid that dep.)
- **Auto-select Kokoro when GPU is available, google_tts when not.** Adaptive selection is a knob; not part of v1.

## Tests (TDD)

`tests/unit/test_kokoro_tts.py`:

1. `test_is_available_returns_false_when_kokoro_onnx_missing` — patch `importlib` so the lazy import fails; assert `is_available()` returns False.
2. `test_is_available_caches_after_first_check` — `is_available()` only does the import probe once.
3. `test_name_includes_voice_id` — `KokoroTTS(voice="af_heart").name == "kokoro:af_heart"`.
4. `test_synthesize_returns_wav_bytes_via_mocked_model` — patch the model factory to return a fake that produces a known numpy array; assert the output is a valid 16-bit mono WAV with the expected sample rate.
5. `test_synthesize_returns_empty_bytes_on_model_failure` — mock model raises; assert `synthesize` swallows + returns `b""`.
6. `test_synthesize_uses_speed_param` — fake model captures call kwargs; assert `speed` passed through.

Also one config-level test:

7. `tests/unit/test_config.py` — add a small case that `voice_tts_provider = "kokoro"` is accepted (no validation error). Already implicitly true since the field is `str`; the test makes the contract explicit.

## Files to change

- [pyproject.toml](../../pyproject.toml) — add `kokoro-onnx>=0.4.0`.
- [Dockerfile](../../Dockerfile) — RUN step to pre-download the Kokoro ONNX + voices files.
- [tests/unit/test_kokoro_tts.py](../../tests/unit/test_kokoro_tts.py) — NEW. 6 tests.
- [tests/unit/test_config.py](../../tests/unit/test_config.py) — extend with 1 test.
- [docs/decisions/voice-latency-phase3-kokoro.md](../decisions/voice-latency-phase3-kokoro.md) — NEW.
- [docs/plans/voice-latency-optimization.md](../plans/voice-latency-optimization.md) — mark Phase 3 shipped, link to the new decision.

## Acceptance

- ✅ All 7 new tests pass; full suite stays green.
- ✅ `pip install -e .` locally pulls in `kokoro-onnx`; importing `KokoroTTS` doesn't error; `is_available()` returns True.
- ✅ Image build downloads the Kokoro model files as a separate cached layer; subsequent rebuilds don't re-download.
- ✅ In production with `VOICE_TTS_PROVIDER=kokoro`, the cascade picks Kokoro first; `KokoroTTS.synthesize()` returns ~22050 Hz / 24000 Hz WAV bytes that the existing mixer plays back without changes.
- ✅ Decision note shipped. Plan archived to "Superseded / historical".

## Operator action

After this ships, to use Kokoro:

```
VOICE_TTS_PROVIDER=kokoro
```

on the poob Komodo stack Environment panel. Save → auto-redeploy. The bot reboots and the cascade prefers Kokoro. Google Cloud TTS stays as the fallback when Kokoro isn't available (graceful degradation).

To roll back: set `VOICE_TTS_PROVIDER=google_tts` (or remove the var; default is google_tts). Save → redeploy. Zero code change.
