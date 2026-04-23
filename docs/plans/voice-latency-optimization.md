---
type: plan
status: active
date: 2026-03-22
tags: [voice, latency, vad, stt, tts]
related: [[voice-architecture]] [[wake-word-dual-gate]] [[wake-word-latency]]
---

# Voice Latency Optimization Plan

## Problem
Current voice pipeline has 3.5-8+ second response latency. The #1 bottleneck is **end-of-speech detection** — energy-based RMS threshold (150.0) can't distinguish speech from breathing/background noise, causing 5-15 second utterance extensions. Secondary bottlenecks: batch STT upload (0.8-1.5s), Edge TTS network round-trip (0.8-1.2s).

## Target
- **Actual latency**: <1.5s (end-of-speech to first audio)
- **Perceived latency**: <0.5s (filler audio masks processing)

## Architecture: Before vs After

### Before (Sequential Batch)
```
User speaks → Energy VAD (broken, 5-15s) → Upload to Groq Whisper (0.8-1.5s)
→ LLM response (0.5-1s) → Edge TTS network (0.8-1.2s) → play
Total: 3.5-8+ seconds
```

### After (Overlapping Stream with Neural VAD)
```
User speaks → Silero VAD (300ms silence timeout) → Filler audio plays instantly
                                                  → Groq Whisper STT (0.8-1.5s)
                                                  → LLM streaming → Kokoro local TTS (50-100ms)
                                                  → Crossfade from filler to response
Total actual: ~1.2-1.8s | Perceived: ~0.3-0.5s
```

## Phase 1: Silero VAD (Highest Impact — Fixes the Core Problem)

### What
Replace `rms_energy()` threshold with Silero VAD v6 neural network.

### Why
- Energy threshold (150.0) triggers on breathing (165-274 RMS), extending utterances indefinitely
- Silero classifies speech vs noise using spectral features, not amplitude
- 87.7% TPR at 5% FPR (vs energy-based ~0%)
- Outputs probability 0.0-1.0 (not binary), enabling hysteresis
- <1ms per frame on CPU via ONNX Runtime

### How
1. **Resample**: 48kHz stereo → 16kHz mono via numpy decimation ([::3] on averaged channels)
2. **Ring buffer**: Accumulate 320 samples/frame → emit 512-sample chunks for Silero (32ms)
3. **ONNX inference**: Run Silero model, get speech probability per chunk
4. **State machine with hysteresis**:
   - Speech start: probability > 0.6
   - Speech end: probability < 0.35 for 300ms
   - Pre-buffer: 300ms of audio before speech start (capture first phonemes)
   - Packet gap: 300ms without Discord packets = immediate end-of-speech

### Dependencies
```
pip install silero-vad onnxruntime numpy
```

### Key Parameters
| Parameter | Value | Rationale |
|-----------|-------|-----------|
| speech_threshold | 0.6 | Rejects breathing (scores ~0.0-0.1) |
| silence_threshold | 0.35 | Hysteresis prevents toggling |
| min_speech_ms | 150 | Filters clicks and pops |
| silence_timeout_ms | 300 | 200ms faster than current 500ms |
| pre_buffer_ms | 300 | Captures speech onset |
| min_interruption_ms | 500 | Prevents stopping on brief noises |

### Files to Change
- `src/poob/voice/audio_buffer.py` — Replace `rms_energy()` VAD with `SileroVADProcessor`
- `src/poob/voice/session.py` — Update echo suppression to use speech probability
- `pyproject.toml` — Add silero-vad, onnxruntime deps

## Phase 2: Filler Audio (Cheapest UX Win)

### What
Play pre-generated "thinking" sounds immediately when end-of-speech fires, masking 1-2s of pipeline processing.

### How
1. Pre-generate 10-15 filler clips at startup using Edge TTS: "Hmm...", "Let me think...", "Good question...", breath sounds
2. Store as WAV files in `data/voice_fillers/`
3. The INSTANT Silero VAD fires end-of-speech, play a random filler
4. When actual response audio is ready, crossfade 50-100ms from filler to response

### Files to Change
- `src/poob/voice/session.py` — Add filler playback in `_on_utterance_detected`
- `src/poob/voice/fillers.py` — New module for filler generation and management

## Phase 3: Local TTS with Kokoro-82M (Eliminate 0.8-1.2s Network Latency)

### What
Replace Edge TTS (network-dependent, 0.8-1.2s TTFB) with Kokoro-82M (local GPU, 50-100ms TTFB).

### Why
- Kokoro-82M: 82M params, ~1.1GB VRAM, 20-35x real-time on GTX 1080
- StyleTTS2 architecture — top-tier quality, ranks above 10x larger models
- Apache 2.0 license, 54 voices, 8 languages
- Time-to-first-audio: <300ms (vs Edge TTS 800-1200ms)

### Dependencies
```
pip install kokoro soundfile
# Also need espeak-ng system dependency
```

### Fallback
Keep Edge TTS as fallback when Kokoro unavailable (GPU busy, model not loaded).

### VRAM Budget (GTX 1080, 8GB)
| Component | VRAM |
|-----------|------|
| Kokoro-82M | ~1.1 GB |
| Available for future local STT | ~3.0 GB |
| OS + Discord + misc | ~3.9 GB |

### Files to Change
- `src/poob/voice/tts.py` — Add KokoroTTSProvider
- `src/poob/main.py` — Wire Kokoro as primary TTS
- `pyproject.toml` — Add kokoro dep

## Phase 4: LLM→TTS Sentence Streaming Overlap

### What
Start TTS on the first complete sentence while LLM continues generating.

### How
- Already partially implemented (sentence streaming in `VoiceConversationManager.generate_streaming`)
- Wire `stream2sentence` library for robust sentence boundary detection
- First sentence plays while LLM generates rest → hides LLM generation time

### Dependencies
```
pip install stream2sentence
```

## Phase 5 (Future): Streaming STT

### What
Replace batch Groq Whisper with streaming STT (Deepgram or local faster-whisper).

### Why
- Eliminates 0.8-1.5s batch upload time
- Transcript ready at end-of-speech, not 1.5s later
- Deepgram: $200 free credit (~433 hours), semantic endpointing
- faster-whisper: Free forever, ~3GB VRAM, 200-500ms latency on GTX 1080

### When
After Phases 1-4 are stable. Current Groq Whisper is good enough with the other optimizations.

## Expected Latency Progression

| Phase | Change | Actual Latency | Perceived |
|-------|--------|---------------|-----------|
| Current | Energy VAD + batch | 3.5-8+s | 3.5-8+s |
| Phase 1 | Silero VAD | ~2.5-3s | ~2.5-3s |
| Phase 2 | + Filler audio | ~2.5-3s | ~1-1.5s |
| Phase 3 | + Kokoro local TTS | ~1.2-1.8s | ~0.3-0.5s |
| Phase 4 | + Streaming overlap | ~1.0-1.5s | ~0.3-0.5s |
| Phase 5 | + Streaming STT | ~0.7-1.0s | ~0.3s |

## Key Research Sources
- Silero VAD: github.com/snakers4/silero-vad (v6.2.1, MIT, 87.7% TPR)
- Kokoro-82M: huggingface.co/hexgrad/Kokoro-82M (Apache 2.0, StyleTTS2)
- Pipecat framework: github.com/pipecat-ai/pipecat (reference architecture)
- LiveKit turn detection: livekit.com (SmolLM v2 EOU model)
- Deepgram streaming: developers.deepgram.com ($200 free credit)
