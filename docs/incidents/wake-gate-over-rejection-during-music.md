---
type: incident
status: resolved
date: 2026-04-22
tags: [voice, wake-word, music]
supersedes: []
related: [[wake-word-dual-gate]] [[voice-architecture]]
---

# Wake-word gate rejected legit addresses while music was playing

## Symptom

User reported Poob "missing wake words" during music. Smoking-gun log:

```
Text wake word match      transcript='Hey, Poob. Play the untold by Succession Studios.'
Text wake word rejected (bot audio active, no acoustic confirmation)
```

Ben's cleanly-spoken request was dropped because music was playing. He had to repeat himself ~40s later, which read to observers as "playing songs twice" — not a duplicate play, an ignore-then-retry pattern.

## Root cause

`session.py:_bot_audio_active` was returning True for three conditions:

1. `self._is_speaking` (TTS in progress) — legitimate loopback risk
2. `self.voice_client.is_playing()` — any Discord VC playback, over-broad
3. `self.music_player.is_playing` — music playing, over-broad

The loopback class this gate defends against is specifically *Poob's own TTS* being re-transcribed via a listener's speakers back into the mic. Music tracks don't contain wake-word phonemes — they aren't a loopback risk. Treating music as bot-audio-active meant "whenever music is playing, demand acoustic confirmation," which in noisy environments (game audio, crosstalk, TV) caused legitimate wake words to be rejected whenever the acoustic model missed.

## Fix

`_bot_audio_active` now returns `self._is_speaking` only. The flag is set around both the standalone-TTS path and the music-overlay path in `_play_audio`, so loopback protection is preserved; music-only playback no longer gates wake detection.

```python
def _bot_audio_active(self) -> bool:
    return self._is_speaking
```

## Related change (same deploy)

Tightened Poob's non-tool casual-chat system prompt from "2-4 sentences is the sweet spot, but go longer if you're on a roll" to "1-2 sentences max, ~20 words." The old rule produced 100+ char replies that spoke for 10–13 seconds via TTS, which was the dominant component of perceived response latency.

## Validation

- 43/43 voice + music_ui tests pass.
- Deploy signal: `Text wake word rejected (bot audio active...)` events should now only appear when the bot is actively TTS-speaking, not when music alone is playing.
