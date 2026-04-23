---
type: decision
status: active
date: 2026-04-22
tags: [voice, tts, audio]
related: [[toob-voice-filter-chain]] [[voice-architecture]]
---

# TTS loudness normalized via FFmpeg `speechnorm` at decode

## Context

Poob's TTS sat audibly quieter than the music bed, even after two rounds of scaling `PCMVolumeTransformer` (2.0 → 2.5 → 3.0). Multiplier-only gain scales peaks, not perceived loudness. The real gap is an RMS / LUFS mismatch:

- **Mastered music** (YouTube / Spotify rips) targets ~-9 to -14 LUFS, heavily compressed — "sits at the ceiling."
- **Raw TTS** (Edge, Google, Kokoro) peaks at ~-16 LUFS with wide dynamic range, designed for speech clarity.

Multiplying a quiet-RMS signal by 2.5 only brings peaks up; the *average* level stays perceptually quieter than the loudness-normalized music it sits next to. Hitting int16 peaks with PCMVolumeTransformer caused clipping artifacts before perceptual loudness caught up.

## Decision

Apply FFmpeg `speechnorm` at the decode step in `session.py:_play_audio` — the single funnel every TTS payload (Poob *and* Toob) flows through.

```python
tts_source = discord.FFmpegPCMAudio(
    tmp_path,
    executable=FFMPEG_PATH,
    options="-af speechnorm=e=12.5:r=0.0001:l=1",
)
```

- `e=12.5` — expansion ceiling; up to 12.5× gain on quiet segments.
- `r=0.0001` — per-frame rise max; prevents audible pumping.
- `l=1` — per-frame peak limiter; prevents clipping before the PCMVolumeTransformer stage.

Single-pass, low-latency (unlike `loudnorm`'s two-pass ~100-200ms overhead), and preserves speech intelligibility.

## Why this layer

`_play_audio` is the one place every TTS byte-stream flows through — both the standalone playback path and the music-overlay path. Applying speechnorm there covers Poob and Toob alike (Toob's filter_chain output also arrives here).

## `PCMVolumeTransformer` → 3.0

After speechnorm, TTS arrives at the PCMVolumeTransformer stage near peak. The extra +1.5 dB from 3.0 pushes speech into gentle clipping, which for voice reads as "fullness" and matches the perceptual loudness of mastered music.

## Alternatives considered

- **Bump the multiplier further.** 4×, 5× — already tried 2.5 and 3.0; further scaling just clips harder without raising RMS.
- **`loudnorm` two-pass.** Correct for broadcast, wrong here — adds 100-200ms latency per utterance, which compounds perceived response time.
- **Lower music default.** Music volume is user-adjustable. Lowering the default would penalize users who want loud music.

## Consequences

- Toob's internal `volume=1.35` pre-gain is now largely absorbed by speechnorm. Kept in place because the bass boost and echo stages in Toob's filter chain reduce perceived loudness; the pre-gain keeps Toob arriving at speechnorm with enough RMS to avoid over-expansion.
- Validation: post-deploy, watch for `Playing audio` + `TTS injected as overlay on music` logs; subjective volume check from the user on first speak.
