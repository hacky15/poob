---
type: decision
status: active
date: 2026-05-23
tags: [voice, latency, tts, kokoro, local-inference]
related: [[voice-architecture]] [[voice-latency-optimization]] [[voice-latency-phase3-kokoro-ship]] [[tts-loudness-speechnorm]] [[deepgram-flux-optional]]
---

# Kokoro-82M as opt-in TTS provider — eliminate cloud-TTS network round-trip when GPU/CPU budget allows

## Context

Phase 3 of [[voice-latency-optimization]]: the existing TTS path goes through Google Cloud TTS (Chirp3-HD-Fenrir voice) or Edge TTS as a fallback. Both ship audio over the network, costing 0.8-1.2 s per spoken response between text-ready and first-audio-frame. With Phase 1 (Silero VAD) shipped and Phase 5 (Deepgram streaming STT) effectively done via the dual-pipeline, TTS is now the single largest remaining contributor to the perceived response latency.

Kokoro-82M is a 82M-parameter local TTS model (StyleTTS2 architecture, Apache 2.0). At ~1.1 GB VRAM on a GPU or ~3-5× real-time on CPU, it produces TTFA under 300 ms — eliminating the cloud round-trip entirely. The `kokoro_onnx` PyPI package wraps the ONNX export with a built-in g2p tokenizer, so we don't need to install espeak-ng at the system level (which the full `kokoro` Python package would require).

The `KokoroTTS` class was already written into [src/poob/voice/tts.py:173](../../src/poob/voice/tts.py) in a prior pass — lazy import of `kokoro_onnx`, lazy model construction, synthesis runs in an executor, returns 24 kHz / 16-bit mono WAV bytes the existing `MixingAudioSource` understands without changes. What was missing for ship: the `kokoro-onnx` dep itself, a Dockerfile pre-download step so first runtime invocation doesn't block on the 350 MB model fetch, unit tests, and this decision note.

## Decision

Ship Phase 3 as an **opt-in** TTS provider via the existing `VOICE_TTS_PROVIDER` env var. Default stays `google_tts` (Chirp3-HD-Fenrir voice — high quality cloud audio). Operators flip to `kokoro` on the Komodo poob stack's Environment panel and redeploy; the cascade then prefers Kokoro and falls back to Google Cloud TTS / Edge TTS when Kokoro fails (model not loaded, GPU busy, etc.).

Concretely:

1. `kokoro-onnx>=0.4.0` added to `pyproject.toml`.
2. `Dockerfile` runs `python -c "from kokoro_onnx import Kokoro; Kokoro()"` after the `pip install` layer to pre-fetch the ~350 MB ONNX model + voices file into the image. Soft-fails (`|| echo "warn: ..."`) so the image still ships when the dep is unavailable; `KokoroTTS.is_available()` correctly returns False at runtime and the cascade picks cloud TTS instead.
3. `tests/unit/test_kokoro_tts.py` — 7 mock-based tests: lazy-import probe (with + without `kokoro_onnx` available), cache-on-first-check, name string includes voice id, synthesize returns valid 16-bit WAV bytes, synthesize swallows model failures returning `b""`, synthesize passes the speed param through.
4. No default-provider flip. Production stays on `google_tts` until evidence shows Kokoro quality holds up in the user's actual voice channels.

## Alternatives considered

- **Make `kokoro` the default immediately.** Rejected: a default change is a behavior change in prod with no preceding A/B. The voice-latency-optimization plan calls for this as the desired end-state but doesn't require it on day one. The opt-in route is reversible by an env-var clear; defaulting is a code change that needs evidence to undo.
- **Use the full `kokoro` Python package** (instead of `kokoro-onnx`). Rejected: pulls in `phonemizer` + system `espeak-ng` install, which Dockerfile would need to add via apt. The ONNX variant has a built-in tokenizer, smaller surface, fewer moving parts.
- **Ship Phase 2 (filler audio) and Phase 4 (sentence streaming overlap) first.** Phase 2's `FillerPlayer` is instantiated but not actively dispatching clips in the response path — a separate ship. Phase 4 is partial and intersects with Phase 3 (sentence-by-sentence streaming TTS is what unlocks the *best* latency win). Doing Phase 3 first gives us a working local TTS to feed into Phase 4 later; doing them in the other order would have us build sentence streaming against the cloud TTS we're about to replace.
- **Adaptive provider selection (Kokoro when GPU available, cloud when not).** Nice-to-have; defer until the env-var manual switch has shown the quality is acceptable.
- **Bigger local TTS** (StyleTTS2 full, Bark, XTTS-v2). All higher quality at 5-50× the VRAM cost. Kokoro's "ranks above 10× larger models" benchmark suggests the quality cliff for 82M params is shallow; ship the small one first.

## Consequences

- ✅ Operators can opt into local TTS via a single env var (`VOICE_TTS_PROVIDER=kokoro`). Quick A/B without code changes.
- ✅ Image size grows by ~350 MB to pre-bake the model. Acceptable; the alternative is a 350 MB blocking download on first bot-spoken response which would feel terrible.
- ✅ Existing cloud cascade is the fallback; no behavior change when Kokoro can't initialize. Graceful degradation by design via `is_available()`.
- ✅ Latency win when used: TTFA drops from ~1 s to <300 ms (CPU) or <100 ms (GPU if homelab's 1080 is available to the container).
- ⚠️ First synthesis after container restart still has a one-time ~1-2 s model load (ONNX session init). After that, every call is fast. Could be mitigated by a warm-up call at boot — deferred until users notice.
- ⚠️ Kokoro voice catalogue is smaller than Google Cloud TTS (54 voices vs 380+). The default `af_heart` voice is high quality but operator can tune via a future `voice_kokoro_voice` config knob (not part of v1; the class already accepts a `voice` arg, just needs config wiring when someone asks).
- ⚠️ Quality may be subjectively below Google Chirp3-HD on long-form expressive sentences. The trade is latency for quality; users who care about quality stay on `google_tts`, users who care about responsiveness flip to `kokoro`. Both options work today.

## Rollback

Single env-var flip: set `VOICE_TTS_PROVIDER=google_tts` (or remove the var; default is google_tts) on the poob stack's Environment panel. Save → auto-redeploy. The cascade goes back to cloud TTS without a code change or rebuild.

If `kokoro-onnx` itself breaks against newer Python / numpy / onnxruntime versions: the lazy import + `is_available()` gate keeps the bot booting; the cascade falls through to cloud TTS automatically. No production crash; users just see the cloud-TTS latency profile until a `kokoro-onnx` upgrade.

## References

- [[voice-latency-phase3-kokoro-ship]] — originating plan + v2 considerations
- [[voice-latency-optimization]] — the broader 5-phase plan
- [src/poob/voice/tts.py](../../src/poob/voice/tts.py) — `KokoroTTS` class + `build_tts_cascade` wiring
- [Dockerfile](../../Dockerfile) — `kokoro_onnx` pre-download step (sibling to the `openwakeword.utils.download_models` step)
- [kokoro-onnx on PyPI](https://pypi.org/project/kokoro-onnx/) + [hexgrad/Kokoro-82M on HuggingFace](https://huggingface.co/hexgrad/Kokoro-82M) — upstream
- [[tts-loudness-speechnorm]] — the speechnorm filter applied to every TTS output; works identically for Kokoro since output is the same WAV-bytes shape
