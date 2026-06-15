---
type: incident
status: resolved
date: 2026-06-15
tags: [music, ytdl, player, latency, temp-files]
related: [[music-player-architecture]] [[speculative-music-wrap]] [[voice-architecture]]
---

# Every voice "play" downloaded the track twice and orphaned a temp file

## Symptom

On every voice music play, the requested track was downloaded **twice** to two
different temp files. One file was played; the other was leaked on disk with no
cleanup path. User-visible effect: ~1–2s of extra start latency. Surfaced by
the 2026-06-15 forensic audit (music subsystem, finding 1).

Evidence (poob_vc_lastnight.log, "Stranger Things" play):

```
02:23:33.383 Downloading track            [music.player]
02:23:33.427 Track pre-downloaded file=/tmp/poob_music_cqd_eik4.webm  [music.ytdl]
02:23:33.437 Pre-downloaded deferred track [music.player]
02:23:34.833 Track pre-downloaded file=/tmp/poob_music_3qz1m6ll.webm  [music.ytdl]   ← 2nd file
```

## Root cause

The deferred-playback path (voice mode: Poob speaks about the song, then music
starts — see [[voice-architecture]]) raced its own download:

1. `play(deferred=True)` fired `_pre_resolve(track)` as a **fire-and-forget**
   task (`self._loop.create_task(...)`) with no handle stored anywhere.
2. `start_deferred()` launched `_player_loop()` a few seconds later (after
   Poob's TTS).
3. The loop's audio acquisition guarded re-download only on
   `not track.local_file and not track.stream_url`. `download_track` *is*
   idempotent on `track.local_file` (ytdl.py:367) — but `_pre_resolve`'s
   download only sets `track.local_file` at the **end**. If the loop reached
   the track while the pre-resolve download was still in flight,
   `track.local_file` was still `None`, so the loop downloaded again.

Two concurrent downloads, two temp files, one orphaned — violating the
[[music-player-architecture]] invariant "temp files must be cleaned up; skipping
cleanup causes disk exhaustion." The prefetch path (`_start_prefetch`) had the
same latent race (it stored `_prefetch_task` but the loop never awaited it).

## Fix

One **dedup'd resolver** that all three callers share, so a track is resolved
exactly once:

- `_ensure_resolving(track)` returns the in-flight resolve task for a `Track`
  instance (keyed by `id(track)`), creating one only if none exists.
- `_resolve_track(track)` does the `download_track` → `resolve_stream_url`
  fallback (idempotent via `download_track`'s `local_file` cache check).
- `_on_resolve_done` drops finished tasks from the in-flight map.
- **Callers:** `play(deferred=True)` registers the resolve; the player-loop
  acquisition **awaits the same task** instead of issuing a bare download;
  `_start_prefetch` stores `_prefetch_task = self._ensure_resolving(next_track)`.
- `destroy()` cancels any in-flight resolves.

The fragile fire-and-forget `_pre_resolve` was **removed entirely** (zero
grep hits). Files: [music/player.py](../../src/poob/music/player.py).

## Validation

`tests/unit/test_music_resolve_dedupe.py`:
- `test_deferred_race_downloads_once` — the exact prod race: deferred play +
  loop acquisition → `download_calls == 1` (would be 2 before the fix).
- dedup of concurrent resolves, stream-URL fallback, in-flight map cleared on
  done, already-resolved track skipped, and a grep-as-test that `_pre_resolve`
  stays gone and the loop uses `_ensure_resolving`.
- Full unit suite green.
