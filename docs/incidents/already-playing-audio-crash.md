---
type: incident
status: resolved
date: 2026-06-02
tags: [music, voice, player, pycord, playback]
related: [[voice-music-common-pitfalls]] [[music-player-architecture]] [[voice-architecture]] [[music-routing-prompt-thoughtfulness]]
---

# Music never plays — "Already playing audio" crashes the player loop

## Symptom

Operator: *"why is it not playing my music."* Production logs (2026-06-02) show the request route correctly, the track download, then the player loop die before a note is heard:

```
03:52:27 [info ] Playing track   [music.player] title='Desiigner - Panda (Kiko Franco & Kubski Remix)'
03:52:27 [error] Player loop crashed [music.player] error='Already playing audio.'
04:21:28 [info ] Playing track   [music.player] title='TIKI TIKI (Slowed)'
04:21:28 [error] Player loop crashed [music.player] error='Already playing audio.'
```

Both songs the operator explicitly asked for ("panda…", "tiki tiki") routed and downloaded fine, then **crashed at playback** — so "play tiki tiki" failed BOTH ways: routing dropped it earlier (a separate hallucination bug, see [[music-routing-prompt-thoughtfulness]]), and when routing finally worked, playback crashed here.

## Root cause

`GuildMusicPlayer._player_loop` called `self.voice_client.play(self._mixer, ...)` ([player.py](../../src/poob/music/player.py)) with **no check that the shared voice client was free**. Pycord's `VoiceClient.play()` raises `ClientException: Already playing audio.` if a source is already playing — and the **voice session plays standalone TTS clips on the same voice client**: the "Playing X" confirmation and spoken chat replies (`discord.voice_cog` "Spoke chat response in VC", `session.py` `vc.play(source)`).

Sequence: user asks for a song → brain routes → the "Playing X" confirmation is spoken in VC (`vc.play(tts)`) → the music player finishes downloading and calls `vc.play(mixer)` while the TTS is still playing → `ClientException` → the `except Exception` in the loop logs "Player loop crashed" and the loop exits → the song never starts.

The asymmetry was the defect: the **voice session already waits** politely (`while self.voice_client.is_playing(): …`) before its own TTS; the **music player did not** — it barged in and crashed. During steady-state music playback this never fires (TTS is overlaid into the mixer with ducking, not a separate `play()`); the collision is specifically at music **start** while a standalone clip is active — exactly the "play X" path.

See the long-standing hazard note: [[voice-music-common-pitfalls]] — *"`play()` while playing raises `ClientException`. The mixer prevents this by being the sole source."* The mixer is the sole source **once music is playing**; the gap was the start transition.

## Fix

New `GuildMusicPlayer._await_voice_client_free()` — before `vc.play(self._mixer)`, wait for `voice_client.is_playing()` to clear (poll every `CLIENT_FREE_POLL_S = 0.05s`), bounded by `CLIENT_FREE_TIMEOUT_S = 8.0s`. The natural case: the short "Playing X" confirmation finishes (~3-5s) and music starts right after. On timeout (a stuck clip), `voice_client.stop()` the lingering source — the explicit play request takes precedence — then proceed. This mirrors the session's own wait pattern, so the two `play()` callers no longer race.

Extracted as a small testable method (matches the `_try_autoplay_inject` pattern) rather than inlining the wait in the 130-line loop.

## Validation

- `tests/unit/test_music_player_autoplay.py`: `test_await_client_free_returns_immediately_when_idle`, `test_await_client_free_waits_then_proceeds`, `test_await_client_free_stops_stuck_clip_on_timeout`.
- Full unit suite green.
- Post-deploy: `Player loop crashed … error='Already playing audio.'` should drop to zero; a "play X" issued while Poob is mid-sentence should start the song right after the confirmation instead of silently dying.

## Follow-ups

- Steady-state music + TTS coexistence (ducking via the mixer overlay) is unchanged and was never the problem. Only the start transition is fixed.
- If the confirmation TTS and the music start feel sequential-and-slow for text-channel plays, a future option is to overlay the confirmation into the mixer rather than playing it standalone — but that's a UX refinement, not a crash fix; not assumed here.
