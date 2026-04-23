---
type: architecture
status: active
date: 2026-04-07
tags: [voice, stt, tts, wake-word]
related: [[poobbrain-architecture]] [[music-player-architecture]] [[wake-word-dual-gate]] [[toob-voice-filter-chain]] [[tts-loudness-speechnorm]]
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

## Toob — evil music spirit

When Poob handles a music `play`/`queue`, Toob responds instead. Separate persona, deeper voice, FFmpeg filter chain. Music *control* commands (skip, pause, stop, volume) execute silently — no TTS, no personality wrap.

Voice signal routing is structural, not string-based. `VOICE_TOOB = "__VOICE_TOOB__"` is yielded as the first item from `respond_streaming()` when Toob should speak. The session switches synth functions based on the signal. No string prefixes, no regex parsing.

See [[toob-voice-filter-chain]] for the FFmpeg stages.

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
- [voice/audio_buffer.py](../../src/poob/voice/audio_buffer.py)
- [voice/stt.py](../../src/poob/voice/stt.py)
- [voice/tts.py](../../src/poob/voice/tts.py)
