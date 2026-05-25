---
type: gotcha
status: active
date: 2026-04-07
tags: [voice, music, pycord]
related: [[voice-architecture]] [[music-player-architecture]] [[toob-voice-filter-chain]]
---

# Voice and music pipeline pitfalls

## Trigger

Writing or modifying anything in `src/poob/voice/` or `src/poob/music/`.

## The list

Each item is a hazard that has bitten us before.

- **`yield from` in async generators is a syntax error.** Use `for item in ...: yield item`.
- **asetrate math is inverted from intuition.** `asetrate` *below* the native rate = pitch DOWN (not up). The filter reinterprets the source rate, so a lower asetrate effectively stretches the audio, which drops pitch. See [[toob-voice-filter-chain]].
- **The brain cascade needs non-Groq fallbacks.** Multi-user voice sessions exhaust Groq's per-key rate limits quickly. Current order: Groq `gpt-oss-20b` → NVIDIA NIM → Groq Scout (last-resort). Cerebras 235B and llama-3.3-70b are both OUT — see [[groq-gpt-oss-20b-swap]] and [[drop-cerebras-from-cascade]] for why. See [[vlm-cascade-operational-findings]] for benchmark history.
- **Scout 17B over-routes to music** — "Did you get offended?" → plays "Big Ole Freak". Keep it last in the cascade.
- **`<function=...>` in LLM output.** Some models emit tool calls as raw text instead of structured `tool_calls`. Regex cleanup in both `respond()` and `respond_streaming()` prevents this markup from reaching Discord.
- **Deepgram transcript replay.** `reset_transcript(user_id)` must be called after utterance emission, not just at utterance start — otherwise the next utterance carries the tail of the previous transcript.
- **Integer overflow in mixing.** `audioop.add()` handles clipping internally; the old numpy path required manual float32 upcasting + `np.clip()`. Don't regress to numpy.
- **Instant volume changes click.** Stepping from 100% to 25% in a single frame causes a transient pop. Gain ramps (300ms) fix this.
- **yt-dlp on the event loop.** Blocks heartbeat, Discord disconnects. Always `run_in_executor`.
- **Stale stream URLs.** YouTube URLs expire ~6 hours. Pre-download eliminates this for normal tracks. Stream URLs only for livestream fallback.
- **`play()` while playing.** Raises `ClientException`. The mixer prevents this by being the sole source.
- **Grace period on overlay.** Cloud TTS buffers; first few `read()` returns are empty. Without the grace period, the mixer kills the overlay before audio starts.
- **BufferedAudioSource: silence vs empty bytes.** Returning `b""` from `read()` tells Pycord the track ended. On buffer underrun, return silence (`b"\x00" * 3840`) to keep the stream alive while the buffer refills.
- **Temp-file cleanup.** `download_track()` creates temp files. They MUST be cleaned up after playback (`cleanup_track_file()`). The player loop, `stop()`, and `destroy()` all handle this. Forgetting cleanup causes disk exhaustion.
- **yt-dlp download extension mismatch.** yt-dlp may change the output file extension (e.g. `.webm` → `.opus`). `download_track()` checks multiple candidates.
- **`is_playing` is a property, not a method.** See [[pycord-is-playing-is-a-property]].

## Reference

Architecture: [[voice-architecture]], [[music-player-architecture]].
