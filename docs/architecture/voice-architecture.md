---
type: architecture
status: active
date: 2026-04-07
tags: [voice, stt, tts, wake-word, vad]
related: [[poobbrain-architecture]] [[music-player-architecture]] [[wake-word-dual-gate]] [[toob-voice-filter-chain]] [[tts-loudness-speechnorm]] [[voice-latency-phase1-silero-reenabled]] [[voice-latency-phase2-filler-dispatch]] [[voice-latency-phase3-kokoro]]
---

# Voice architecture — dual pipeline, Toob, deferred playback, multi-provider cascade

## Purpose

Real-time voice interaction with Poob in Discord voice channels: wake-word detection, streaming STT, LLM response, TTS playback, mixing with music.

## Shape

```
User speaks in VC
    │
    ▼
Pycord Sink → PCM frames per user
    │
    ▼
DualPipelineProcessor
    │   ├── OpenWakeWord (acoustic, local, ~50ms)
    │   └── Deepgram streaming STT (transcript ready when speech ends)
    │
    ▼  (dual-gate: see wake-word-dual-gate)
on_dual_addressed(user_id, transcript)
    │
    ▼
PoobBrain.respond_streaming()  → sentences yielded
    │
    ▼  (per-sentence)
_synthesize (Poob) or _synthesize_toob (Toob)
    │
    ▼
_play_audio → FFmpegPCMAudio(+speechnorm) → PCMVolumeTransformer(3.0)
    │
    ├── Music not playing → vc.play(source)
    └── Music playing → music_player.inject_tts_overlay(source) → mixer ducks music
```

## Toob — evil music spirit (and Boob, Toob's side piece)

When Poob handles a music `play`/`queue`, **Toob** responds instead — separate persona, deeper voice, FFmpeg filter chain. Music *control* commands (skip, pause, stop, volume) execute silently — no TTS, no personality wrap.

About 1 play in 20 surfaces **Boob** instead — Toob's sweet side piece. Higher pitch, slightly faster, three sentences, compliments instead of mocks, always self-introduces as Toob's side piece. Same architectural surface as Toob (separate prompt, separate filter chain, separate Google Chirp voice — Leda) but lives on the same one-bit dispatch in `respond_streaming`. See [[boob-music-wrap-variant]].

Voice signal routing is structural, not string-based. `VOICE_TOOB` and `VOICE_BOOB` are sentinels yielded as the first item from `respond_streaming()`. The session loop's synth dispatch is keyed on a `voice_persona` string (`"poob" | "toob" | "boob"`) so adding a fourth persona is a one-line addition to the table. No string prefixes, no regex parsing.

See [[toob-voice-filter-chain]] for Toob's FFmpeg stages and [[boob-music-wrap-variant]] for Boob's.

## Silent music controls

Control commands return `[SILENT]Status` from the music handler. The brain detects the prefix, returns empty string to the session → no TTS generated, no personality wrap. Action executes instantly (~600ms end-to-end). The `[SILENT]` protocol is a structured contract between handler and brain, not a bandaid.

## Deferred playback — session-level orchestration

When a user says "play X" in voice, the music should start AFTER Toob finishes speaking, not immediately.

Pure state inspection, no flags:

1. Music handler queues the track with `deferred=True` → URL pre-resolves but playback doesn't start.
2. Toob responds → TTS plays → session checks: `music_player.is_playing == False AND queue not empty`.
3. If true → `music_player.start_deferred()` → music begins.

Previous versions used a `_music_deferred_pending` flag on PoobBrain, which was shared mutable state across all users/guilds — a race. Observing player state directly eliminates the coupling.

## Multi-provider tool-calling cascade

Groq 70B (primary) hits 429s in multi-user sessions. Cascade order in `_groq_with_tools`:

1. Groq llama-3.3-70b-versatile (best accuracy)
2. Cerebras qwen-3-235b (different provider, avoids Groq rate limits)
3. NVIDIA NIM qwen3-next-80b (third provider)
4. Groq llama-4-scout-17b (last resort — over-routes to music; keep last)

See [[vlm-cascade-operational-findings]] for the benchmark data behind the ordering.

## Music state in system prompt

When music is playing, the system prompt is dynamically extended with the current track and a directive list: "when music is playing and the user says skip/pause/stop/volume/etc., you MUST call music_assistant". This gives the LLM context to route "skip" correctly without keyword matching — without music playing, "skip" is just a word.

## Wake word stripping in music handler

The tool-caller passes the full user message including "Hey, Poob." to the music handler. Without stripping, YouTube searches for "Hey, Poob. Stop." find random videos. Regex strip at the top of `handle_music_request()`:

```python
cleaned = re.sub(
    r'^(?:hey[,.]?\s*)?(?:poob|poop|pub|boob)[,.]?\s*',
    '', request, flags=re.IGNORECASE,
).strip()
```

## Programmatic personality variance — 90 POOB_STATES

Monolithic personality prompts degrade into repetitive caricatures. The model sees its own intense responses in chat history and overfits to them. Fix: 90 random "vibe" states injected per-call at the code layer:

```python
state = random.choice(POOB_STATES)
prompt += f"\n\n[YOUR CURRENT VIBE (embody this in your tone, do NOT mention or describe it): {state}]"
```

Because the vibe changes every turn, even if history is full of paranoid responses, the new prompt might say he's mourning a dust mite — forcing a tone shift.

Rules in the system prompt: answer what was asked FIRST, then let vibe color delivery. 1-2 sentences max. Never mention or describe the vibe — embody it.

## End-of-speech detection (VAD)

Two paths, operator-selected at deploy time via `VOICE_USE_SILERO_VAD`:

- **Energy-RMS (default).** `UserAudioBuffer` uses a packet-gap + RMS-threshold gate. Cheap (no model), reliable on loud unambiguous speech, but has a long failure tail (5-15 s utterance extension on quiet trailing speech, false-fires on loud non-speech like keyboards/music).
- **Silero VAD (opt-in).** `SpeechDetector` routes Discord frames through a single per-session `SileroVADProcessor` instance. The processor maintains `_PerUserSileroState` (per-user 320-sample ring buffer + cloned LSTM `_state`/`_context` snapshot) so the model is shared across users without bleeding acoustic context. Model load is deferred to the first speech frame to avoid stalling multi-user joins. Re-enabled 2026-05-24 — see [[voice-latency-phase1-silero-reenabled]]. Default flip pending operator A/B.

`get_or_create_buffer` (in `session.py`) is the single switch — it returns either path based on `use_silero_vad`. Downstream code (utterance callback, STT dispatch) is path-agnostic.

## Filler audio (perceived-latency mask)

After end-of-speech fires, `_process_single_response` dispatches `_maybe_play_filler()` via `create_task` before queueing the real LLM-driven response. Pre-rendered filler clips ("hmm", "let me think...") play immediately via the shared `_play_audio` overlay so music ducks naturally; the real response queues behind the filler via the existing `voice_client.is_playing()` mutex. Shipped 2026-05-24, see [[voice-latency-phase2-filler-dispatch]].

## Wake word detection

Details in [[wake-word-dual-gate]]. Summary:

- Dual-gate: acoustic (openwakeword) + text-regex, with context-aware relaxation when bot is silent.
- Threshold 0.7 (up from 0.5) — reduces false positives in multi-user calls.
- Pending wake timeout 1.5s (down from 3s) — stale detections don't carry forward.
- 15s max utterance duration — prevents 47-second accumulated transcripts from being treated as single commands.
- Deepgram keyterms — `play`, `skip`, `stop`, `pause`, `volume`, `shuffle` boosted for STT accuracy.

## Invariants

- `_is_speaking` flag covers TTS (both standalone and music-overlay paths). It's the signal for loopback gating — see [[wake-gate-over-rejection-during-music]].
- One response pipeline at a time per session (`_response_lock`). STT runs in parallel; responses serialize.
- Addressed-utterance queue caps at 5 — requests arriving while Poob is responding queue rather than drop, FIFO.

## Key files

- [voice/session.py](../../src/poob/voice/session.py)
- [voice/dual_pipeline.py](../../src/poob/voice/dual_pipeline.py)
- [voice/address_detector.py](../../src/poob/voice/address_detector.py)
- [voice/audio_buffer.py](../../src/poob/voice/audio_buffer.py) — energy-RMS VAD path
- [voice/silero_vad.py](../../src/poob/voice/silero_vad.py) — Silero VAD path (opt-in)
- [voice/fillers.py](../../src/poob/voice/fillers.py) — pre-rendered filler clip player
- [voice/stt.py](../../src/poob/voice/stt.py)
- [voice/tts.py](../../src/poob/voice/tts.py)
