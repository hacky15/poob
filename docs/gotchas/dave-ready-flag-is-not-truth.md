---
type: gotcha
status: active
date: 2026-04-29
tags: [voice, dave, pycord, voice-compat]
related: [[dave-timeout-fail-hard-regression]] [[voice-architecture]]
---

# `dave_session.ready` is NOT a reliable proxy for "audio works"

## Trigger

Any code path that reads `vc.dave_session.ready` (or equivalent) and uses it as a precondition for starting / continuing voice recording, decryption, or STT.

## Why it bites

On this deployment (homelab + Discord region), `dave_session.ready` can stay `False` for the entire call duration even while:

- Opus frames are being decrypted and decoded successfully.
- Deepgram receives clean PCM and produces accurate transcripts.
- Wake words fire, replies are spoken, music plays.

The `ready` flag tracks an internal MLS-handshake state inside the `voice_compat` DAVE patch. It's not a "you can decode now" signal — keys can arrive and decoding can stabilize without the flag flipping. The voice_compat patch sits between Pycord and DAVE; it has its own state machine that doesn't always update the `ready` attribute synchronously with actual decryption capability.

Empirically, the early seconds after a DAVE-enabled join produce a burst of `discord.opus "corrupted stream"` warnings — these are frames that arrived before keys, NOT a permanent failure. The decoder stabilizes once keys land.

## Don't

- Treat `dave_session.ready == False` after a timeout as a fatal condition.
- Refuse to call `vc.start_recording` based on the flag.
- Force-disconnect the voice client because DAVE "didn't complete." (See [[dave-timeout-fail-hard-regression]] for what happens when you do — every `/join` auto-leaves.)
- Use the flag in any branch that throws away audio frames or aborts a session.

## Do

- Wait a reasonable window (~15s) for `ready` to flip — it's still a useful signal when it DOES become True (logs the elapsed_ms cleanly).
- After timeout, log a warning and **start recording anyway**. The decoder will catch up.
- Suppress / route the early opus `corrupted stream` warnings into a less-noisy log level if they're cluttering investigations — they are not actionable.
- Treat actual STT failures (no transcripts after speech) or actual silence-from-decoder as the real signal, not the ready flag.

## Reference

- Incident: [[dave-timeout-fail-hard-regression]] (April 29 2026 — `a2d4041` made the flag fatal, every /join auto-disconnected, reverted in `bb51d8e`).
- Architecture: [[voice-architecture]] — voice_compat DAVE patch, RealtimeAudioSink, dual pipeline.
- Code: `src/poob/discord_bot/cogs/voice_cog.py:join_voice` for the handshake-wait block; `src/poob/voice/voice_compat.py` for the patch internals.
