---
type: decision
status: active
date: 2026-04-22
tags: [voice, toob, tts, ffmpeg]
related: [[voice-architecture]] [[tts-loudness-speechnorm]]
---

# Toob voice — FFmpeg warlord filter chain

## Context

When Poob handles a music `play/queue`, his evil cousin Toob responds instead. Toob needs a distinct, menacing voice, built on top of a normal TTS provider.

## Decision

Apply a chained FFmpeg filter to the TTS output in `_synthesize_toob`:

```
asetrate=18500, aresample=24000,
atempo=2.0,
vibrato=f=5.5:d=0.15,
bass=g=6:f=80,
aecho=0.8:0.85:40:0.3,
volume=1.35
```

Stage rationale:

- **`asetrate=18500`** on a 24kHz source reinterprets the sample rate → pitch drops ~-4.5 semitones. Lower values go deeper; earlier `16000` was too "Darth Vader" and sacrificed intelligibility.
- **`aresample=24000`** brings the sample rate back to playable.
- **`atempo=2.0`** speeds delivery up. 2.0 is the per-stage maximum; chaining two `atempo` stages is the escalation path if more speed is needed.
- **`vibrato=f=5.5:d=0.15`** — subtle pitch wobble. 5.5 Hz sits in natural human prosody range (4-7 Hz); depth 0.15 is shallow enough to stay menacing, not drunk.
- **`bass=g=6:f=80`** — moderate bass boost. Earlier `g=10` over-emphasized the deep-pitch effect; reducing alongside the shallower asetrate keeps the timbre balanced.
- **`aecho=0.8:0.85:40:0.3`** — cavernous reverb.
- **`volume=1.35`** — upstream pre-gain. Mostly absorbed by [[tts-loudness-speechnorm]] downstream, but retained because the bass/echo stages reduce perceived loudness and the pre-gain keeps speechnorm from over-expanding.

## Voice signal routing

`VOICE_TOOB = "__VOICE_TOOB__"` is yielded as the first item from `respond_streaming()` when Toob should speak. The session detects the constant in its async iteration loop and switches: `synth = self._synthesize_toob if use_toob_voice else self._synthesize`. No string prefixes, no `[TOOB]` markers, no regex parsing — the tool call itself is the routing signal.

## Alternatives considered

- **A separate TTS model.** Overkill; FFmpeg filtering on a standard voice is cheap and reversible.
- **Keeping the original deeper config** (`asetrate=16000`, `atempo=1.85`, `bass=10`). User feedback: too deep, too slow, hard to make out.
- **Chained atempo (e.g., `atempo=2.0,atempo=1.1`)** for even faster delivery. Not needed yet; reserve for escalation.

## Consequences

- FFmpeg processing adds ~100-230ms (cold start ~770ms previously — see prewarm note below). Acceptable for the character effect; Toob is only active on music-play requests where some delay is already expected.
- Fallback: if FFmpeg fails, raw Enceladus audio is used; if TTS fails entirely, regular Poob voice.
- Word-length cap in `_wrap_music_response`: "ONE short sentence. 8-12 words MAX" + `toob_max_tokens = min(max_tokens, 60)` to prevent drift past the word limit at high temperature.

## Startup prewarm (2026-04-23)

`_prewarm_ffmpeg()` runs at `session.py` module import. Two passes: (1) `ffmpeg -version` pulls the binary into OS page cache, (2) a 0.3-second silence through the real Toob filter chain primes FFmpeg's filter-graph initialization path. Total ~150-400ms of startup cost, amortized into container-boot time nobody waits on. Kills the 770ms cold start on the first post-deploy Toob speak — subsequent invocations were already ~100-230ms.

The module-level `_TOOB_FILTER_CHAIN` constant is now the single source of truth; `_synthesize_toob` references it directly so the prewarm and live paths never drift.
