---
type: gotcha
status: active
date: 2026-05-12
tags: [music, ffmpeg, audio, ux]
related: [[music-on-the-fly-filter-respawn]] [[music-filter-presets]]
---

# Applying an audio effect or replay/previous creates a ~200-400 ms audio gap

## Trigger

User in voice says "nightcore it" while a song is playing. The music keeps playing for 1-2 more seconds, then there's a noticeable silence (~200-400 ms), then the song resumes nightcored at roughly the same position. Or: user clicks the ``replay`` / ``previous`` buttons (when the now-playing embed lands) and notices the same silence.

You will be tempted to "fix" the gap by:

- Tightening ``BufferedAudioSource.PREFILL_FRAMES``.
- Pre-spawning the next FFmpeg subprocess speculatively.
- Hot-swapping ``voice_client.source`` without a stop / play cycle.
- Removing the ``await self._next_event.wait()`` in ``_player_loop``.

**Don't.** The gap is intentional and load-bearing.

## Why it happens

``discord.FFmpegPCMAudio`` bakes its ``-af`` filter chain at process spawn (in ``before_options`` / ``options``). The only way to change a filter mid-stream is to stop the current FFmpeg subprocess and spawn a new one. That subprocess needs to:

1. Parse the new filter graph.
2. Open the input (local file is fast; network stream is slow).
3. Seek to the listener-perceived position via ``-ss <pos>``.
4. Decode the first ~10 frames (~200 ms) so the BufferedAudioSource prefill can complete before Pycord starts reading.

Steps 1-3 are pure FFmpeg startup cost; step 4 is the unavoidable buffer prefill. Together they're the ~200-400 ms gap. The gap is *less* visible on local pre-downloaded files (~200 ms) and *more* on network streams (~400 ms), so the user-perceived range varies by source.

## Don't

- **Don't reduce ``PREFILL_FRAMES``.** The 200 ms buffer is what protects against audio-thread underrun every 20 ms read; cutting it produces stutter on the entire track, not just the respawn moment.
- **Don't pre-spawn a parallel FFmpeg with the new effect.** You don't know which effect the user will pick next. Even if you did, you'd consume 2× the FFmpeg subprocesses per track.
- **Don't try to hot-swap ``voice_client.source`` without ``vc.stop()``.** Pycord's audio thread holds a reference to the source and will keep reading from the old one; the new one never starts.
- **Don't skip the ``await self._next_event.wait()`` in ``_player_loop``.** That's the synchronization point with Pycord's audio-thread ``after`` callback — without it you have a race between mixer cleanup and the new source's first ``read()``.

## Do

- **Document the gap as part of the UX.** Users won't be surprised once they know it's how filter toggling works.
- **Use the position tracker faithfully.** ``GuildMusicPlayer.position_seconds`` reports listener-perceived position (wall-clock minus paused intervals plus seek offset). Pass it to ``_make_audio_source(seek_seconds=...)`` so the new subprocess resumes at exactly where the old one stopped.
- **Reset position state in the right spots.** At respawn (new ``-ss`` baseline) AND at track boundary (zero everything). See ``_player_loop`` in [music/player.py](../../src/poob/music/player.py).
- **If a user complains "the music skips when I change effects":** that's the gap, working as designed. Show them the [music-on-the-fly-filter-respawn](../decisions/music-on-the-fly-filter-respawn.md) decision note. The only ways to make it shorter than ~200 ms are (a) migrate to Lavalink (separate magnitude of work), or (b) build a custom FFmpeg-pool that keeps subprocesses warm (high complexity, low payoff).

## Reference

- Producers of the gap: ``GuildMusicPlayer.replay``, ``GuildMusicPlayer.previous``, ``GuildMusicPlayer.set_effect``.
- Consumer mechanism: the inner ``while`` in ``GuildMusicPlayer._player_loop`` (see [music-on-the-fly-filter-respawn](../decisions/music-on-the-fly-filter-respawn.md)).
- Roadmap research that established the constraint: [music-bot-feature-roadmap](../research/music-bot-feature-roadmap.md) § "On-the-fly toggling".
