---
type: decision
status: active
date: 2026-05-12
tags: [music, ffmpeg, audio-effects, player-loop, architecture]
related: [[music-filter-presets]] [[music-queue-primitives]] [[music-player-architecture]] [[ffmpeg-effect-toggle-creates-audio-gap]]
---

# On-the-fly audio-effect change — FFmpeg respawn with `-ss` seek

## Context

The roadmap [[music-bot-feature-roadmap]] research established a hard truth: **``discord.FFmpegPCMAudio`` filters bake in at process spawn via ``-af`` in ``before_options`` / ``options``. You cannot change a filter mid-stream without respawning the subprocess.** ``PCMVolumeTransformer`` is the one in-process exception; it scales samples without re-spawning. Everything else — nightcore, slowed, reverb, replay, previous — requires a new FFmpeg invocation.

For replay / previous (#1 in the roadmap, queue primitives) AND filter presets (#4) AND seek (#7, future), the same machinery is required: stop the current source, build a new one with ``-ss <pos>`` and optionally ``-af <chain>``, play it. The pattern bears a known ~200-400 ms gap. Lavalink does live filter updates natively, but migrating to Lavalink is a different magnitude of work.

## Decision

A respawn-aware ``_player_loop`` driven by a single ``_respawn_request`` field on ``GuildMusicPlayer``. Three caller methods produce respawn requests; the loop consumes them via an inner respawn loop that rebuilds the source without advancing the queue.

### Producers

| Caller | Sets ``_respawn_request`` to |
|---|---|
| ``replay()`` | ``(queue.current, 0.0, active_effect_chain)`` |
| ``previous()`` | ``(popped_history_track, 0.0, active_effect_chain)`` — after swapping ``queue.current`` and pushing old current to the queue front |
| ``set_effect(name)`` | ``(queue.current, position_seconds, new_chain)`` |

All three follow the same shape: mutate any necessary queue state synchronously, set ``_respawn_request``, call ``voice_client.stop()`` to wake the loop.

### Position tracker (new)

``_track_started_at``, ``_paused_at``, ``_total_pause_seconds``, ``_track_seek_offset`` fields on ``GuildMusicPlayer``. ``position_seconds`` property returns:

```
wall_clock_since_play - total_pause_seconds - in_flight_pause + seek_offset
```

Wall-clock is set in the player loop right after ``voice_client.play()`` — close enough to listener-perceived t=0. Pause / resume update ``_paused_at`` / ``_total_pause_seconds`` so positions don't drift forward during a pause. The ``seek_offset`` field carries the ``-ss`` value baked into the current FFmpeg invocation so the position reports listener-time, not subprocess-uptime.

### Consumer — inner loop

Within ``_player_loop``, the existing per-track block now has an inner ``while`` that rebuilds the source when ``_respawn_request`` is non-``None`` after the ``await self._next_event.wait()`` returns. The outer loop only advances the queue when the inner loop exits via ``break`` (track ended naturally or skip requested).

```python
while not self._destroyed:
    track = ...  # get_next or use queue.current
    seek_seconds = 0.0
    effect_chain = self._active_effect_chain
    respawning = False
    while not self._destroyed:
        buffered = self._make_audio_source(
            track, seek_seconds=seek_seconds, effect_chain=effect_chain,
        )
        self._mixer = MixingAudioSource(buffered, volume=self._volume)
        self._track_seek_offset = seek_seconds
        # ... vc.play(self._mixer, after=callback) ...
        self._track_started_at = time.monotonic()
        if not respawning:
            self._start_prefetch()
        await self._next_event.wait()
        # ... cleanup mixer ...
        if self._respawn_request is not None:
            new_track, seek_seconds, effect_chain = self._respawn_request
            self._respawn_request = None
            track = new_track  # caller already updated queue.current
            respawning = True
            continue
        break  # natural end / skip
    # ... advance queue ...
```

Key invariants:

- **Prefetch only on the first source per track.** Respawns are mid-track; re-triggering the next-track download wastes bandwidth.
- **Don't ``cleanup_track_file(track)`` on respawn.** The new source needs the same local file (for ``replay`` / ``set_effect``) or a different cached file (for ``previous``); cleanup happens after the inner loop exits.
- **Position-tracker state resets at respawn AND at the track boundary.** Respawn re-anchors ``_track_started_at`` (effective t=0 = the ``-ss`` offset); the track-boundary advance clears all four fields.

### `_make_audio_source` signature change

```python
def _make_audio_source(
    self,
    track: Track,
    *,
    seek_seconds: float = 0.0,
    effect_chain: str | None = None,
) -> discord.AudioSource:
```

``seek_seconds > 0`` adds ``-ss <s>`` to ``before_options`` (fast keyframe-aligned seek). ``effect_chain`` non-empty appends ``-af "<chain>"`` to ``options``. Defaults are no-ops — existing first-time-per-track play path keeps its original FFmpeg invocation.

## Alternatives considered

- **Reset the player loop entirely on each respawn.** Defeats the point — we'd lose the prefetched next-track download, the mixer's TTS-overlay state, and the per-track cleanup discipline.
- **Use ``PCMVolumeTransformer`` for everything.** Only scales sample volume — can't pitch-shift, reverb, etc. Wrong tool.
- **Implement filter toggling by swapping ``voice_client.source`` mid-play.** Pycord/discord.py doesn't support hot-swapping the source attribute on a running playback; the source object's ``read()`` is called from a non-asyncio thread.
- **Migrate to Lavalink for live filter updates.** Big effort, JVM RAM (300-500MB), and music-overlay support is weaker. Deferred per the roadmap.

## Trade-offs the user accepts

- **~200-400 ms audio gap** on every respawn. Documented in [[ffmpeg-effect-toggle-creates-audio-gap]]. Mitigation: BufferedAudioSource's prefill (200ms) hides most of it once the new subprocess is warm; the perceptible gap is closer to 200 ms in practice.
- **Position drift on long pauses.** Wall-clock tracker is monotonic, so timezone changes / system suspend don't break it. But if FFmpeg's audio stream lags behind wall-clock (e.g. network reconnect on a stream URL), ``position_seconds`` over-reports. Acceptable: respawn seeks to the over-reported position, the user hears a tiny bit ahead. Not worth instrumenting FFmpeg's PTS to fix.
- **Network streams may not support fast seek.** ``-ss`` before-input seek on a non-seekable stream can fail or restart from 0. Local pre-downloaded files (the common case) are fine. Falls back to position 0 if seek fails; user hears the track restart instead of jumping forward.

## Validation

- 14 tests in ``tests/unit/test_music_player_primitives.py`` cover position-tracker math (wall-clock, pause-accumulation, in-flight pause) and respawn-request state transitions.
- Smoke test in prod: change effect mid-track and confirm a ~200-400 ms gap, then the track resumes at the same musical position with the new filter audible.

## Rollback

Revert the inner loop in ``_player_loop`` back to the single source path; drop ``_respawn_request`` reads. Position-tracker fields can stay (zero cost). ``replay`` / ``previous`` / ``set_effect`` will return ``None`` or fail silently because their respawn requests will be ignored — caller still sees the queue mutations they make, just without the audio re-render.
