---
type: architecture
status: active
date: 2026-04-07
tags: [music, audio, ytdl, ffmpeg]
related: [[voice-architecture]] [[one-handler-music-contract]] [[poobbrain-architecture]] [[music-autoplay-cascade]] [[music-named-playlists]] [[music-effect-stacking]]
---

# Music player — yt-dlp + FFmpeg + audioop PCM mixer

## Purpose

Play music in Discord voice channels, mixed with Poob/Toob TTS, with low stutter and controllable from voice / text / button. No JVM overhead (Lavalink rejected).

## Why not Lavalink

Lavalink is the standard option but loses here: single-server bot (no distributed audio), 300-500 MB JVM RAM, FFmpeg is already a dependency for other audio work, and the mixer *needs* raw PCM access. Lavalink doesn't support mixing natively; only NodeLink does and it adds another moving piece. Lavalink's seeking/filter advantages don't apply to our use case.

## Shape — four-layer anti-stutter architecture

Choppy audio had four independent causes. Each fix lives at the right layer.

**Layer 1 — Pre-download** (eliminates YouTube TLS termination):
YouTube CDN kills TLS sessions after ~3-4 minutes ([yt-dlp #8854](https://github.com/yt-dlp/yt-dlp/issues/8854)). FFmpeg reconnect can't always recover because the URL may have expired. Fix: `AsyncYTDL.download_track()` pre-downloads audio to a temp file before playback. FFmpeg reads from local disk — zero network dependency during playback. Livestreams fall back to streaming with reconnect flags. Temp files cleaned up after each track finishes or on stop/destroy.

**Layer 2 — BufferedAudioSource** (absorbs I/O jitter):
Pycord's audio thread calls `read()` every 20ms. If FFmpeg's pipe blocks, the frame is late. `BufferedAudioSource` wraps `FFmpegPCMAudio` with a dedicated reader thread filling a 100-frame (2-second) queue. The audio thread reads from the queue (always fast), never the pipe directly. Returns silence on underrun (not empty bytes — empty signals EOF to Pycord). Prefills 10 frames (200ms) before playback starts.

**Layer 3 — audioop mixer** (eliminates per-frame allocations):
Replaced numpy with `audioop` (C extension, `audioop-lts` on Python 3.13+). `audioop.mul()` for volume scaling, `audioop.add()` for mixing — both operate directly on bytes with built-in int16 clipping. Zero per-frame allocations, ~5-10× faster than numpy for 1920-sample buffers. Eliminates GC pressure that caused frame drops.

**Layer 4 — Python 3.13 timer resolution**:
Python 3.13 uses `CREATE_WAITABLE_TIMER_HIGH_RESOLUTION` on Windows — ~100ns sleep accuracy vs. the old 15.6ms default. No code changes needed; requires Python 3.11+.

## MixingAudioSource — PCM mixer

Discord allows exactly ONE audio stream per bot per guild. `VoiceClient.play()` raises `ClientException` if called while playing. Solution: a custom `discord.AudioSource` that mixes music + TTS in real-time using audioop.

1. Music plays through `MixingAudioSource` as the primary source.
2. When Poob needs to speak, TTS audio is injected as an "overlay" via `play_overlay()`.
3. The mixer's `read()` method (called 50×/sec by Pycord) reads both sources, ducks music to 25% volume via `audioop.mul()`, sums with `audioop.add()` (built-in int16 clipping), returns a mixed frame.
4. When TTS ends (grace period of 8 empty reads = 160ms), music volume ramps back up.

Critical details:

- **audioop handles clipping** — `audioop.add(a, b, 2)` clips int16 overflow internally in C. No manual clip step.
- **Gain ramps** — 15 frames (300ms) fade prevents audible click/pop on volume changes.
- **Grace period** — 8 empty overlay reads before cleanup, prevents premature TTS kill during FFmpeg buffering.
- **Frame size** — exactly 3840 bytes per `read()` (20ms at 48 kHz stereo 16-bit), enforced by Pycord.
- **Thread safety** — `read()` runs in Pycord's audio daemon thread; overlay injection from asyncio thread is safe because the Python GIL makes single-pointer writes atomic.

## yt-dlp — pre-download + streaming fallback

Primary path: `download_track()` downloads audio-only to a temp `.webm` file (2-5s for typical tracks). FFmpeg reads from disk — immune to CDN jitter, TLS termination, URL expiration. Download latency is hidden by the pre-fetch system (next track downloads while current plays).

Fallback: for livestreams (`track.is_stream`) or download failures, `resolve_stream_url()` + streaming with FFmpeg reconnect flags. Both paths go through `BufferedAudioSource`.

FFmpeg flags (streaming): `-nostdin -probesize 1000000 -analyzeduration 0 -reconnect 1 -reconnect_streamed 1 -reconnect_on_network_error 1 -reconnect_delay_max 5`. The `-probesize 1000000 -analyzeduration 0` eliminates the initial burst-then-stall pattern. `-reconnect_on_network_error 1` handles TCP/TLS resets. `-nostdin` prevents blocking on stdin (Windows issue).

FFmpeg flags (local file): just `-nostdin`. Disk reads are instant.

All yt-dlp calls run in ThreadPoolExecutor (3 workers, dedicated pool) — yt-dlp is synchronous and will block the event loop / kill Discord heartbeat on the main thread.

## FFmpegPCMAudio, not FFmpegOpusAudio

`FFmpegOpusAudio` is more CPU-efficient (Opus passthrough, no re-encoding) but is **incompatible with PCM mixing**. The mixer needs raw int16 samples to sum. PCM decode/encode CPU cost for one stream on modern hardware is negligible.

## Queue

List-backed (not deque) — queues need random access for display pagination, `random.shuffle()`, and remove-by-index. `pop(0)` performance on sub-500 queues is negligible.

Loop modes: `OFF → LOOP_ONE → LOOP_QUEUE`, resolved in a single `get_next()` method.

Shuffle: Fisher-Yates with saved original order. Unshuffle restores only the remaining tracks.

Transitions: event-driven via `asyncio.Event`. The `after` callback in `vc.play()` fires in FFmpeg's reader thread and calls `loop.call_soon_threadsafe(event.set)` to unblock the async player loop. No polling.

## Audio-effect pipeline (on-the-fly filter toggle)

Effects are **parametric**: each adjustable filter (speed/bass/reverb/8d/tremolo/vibrato) is a continuous *level* in ``effects.DIMENSIONS`` with a chain builder, not a frozen preset string. ``GuildMusicPlayer`` holds ``_effect_levels`` (dimension → level) + ``_atomic_effects`` (presets with no single knob: darth_vader, overload); ``effects.render_effect_chain`` builds the combined, category-ordered ``-af``. Named presets are shortcuts that set levels (``PRESET_DIMENSION_LEVELS``). At most one occupant per category (dimension or atomic). Mutators: ``set_effect`` (replace all), ``add_effect`` (layer), ``remove_effect`` (drop one), ``adjust_effect(target, "up"/"down")`` for "more/less" and "slower/faster" (steps a level via ``effects.step_level``, clamped) — all resolve aliases/dimensions and respawn through the shared ``_apply_effect_chain``. See [[music-effect-stacking]]. Live changes respawn FFmpeg with ``-ss <position>`` and the new combined ``-af`` — see [[music-on-the-fly-filter-respawn]]. The same respawn mechanism powers ``replay()`` and ``previous()``; all callers set ``_respawn_request`` and let the player loop's inner respawn loop rebuild the audio source without advancing the queue.

A ~200-400 ms audio gap on respawn is intentional, documented in [[ffmpeg-effect-toggle-creates-audio-gap]]. The position tracker (``GuildMusicPlayer.position_seconds``) is wall-clock since play minus paused intervals plus the active ``-ss`` offset — listener-perceived position, not subprocess uptime.

## Tool-action surface (music_assistant)

24 actions total, dispatched by ``MusicCog.handle_music_request`` against structured tool args from the brain:

| Group | Actions |
|---|---|
| Playback | ``play``, ``queue_many``, ``skip``, ``previous``, ``replay``, ``pause``, ``resume``, ``stop`` |
| Queue manip | ``move``, ``remove``, ``clear``, ``shuffle``, ``loop`` |
| Volume | ``volume``, ``volume_up``, ``volume_down`` |
| Display | ``now_playing``, ``queue`` |
| Effects | ``apply_effect`` (``effect`` + ``mode``: ``add`` default / ``replace`` / ``remove`` / ``more`` / ``less`` — parametric, on the fly; 'slower'/'faster' need no mode), ``list_effects`` |
| Position | ``seek`` (multi-format ``time`` string via [[music-seek]]) |
| Autoplay | ``autoplay`` (``mode`` arg: ``on`` / ``off`` / ``status``) |
| Playlists | ``save_playlist``, ``load_playlist``, ``list_playlists``, ``delete_playlist`` (``name`` arg for save/load/delete) |
| Spotify import | ``queue_spotify_playlist`` (``url`` arg; ``SpotifyPlaylistResolver`` does the metadata fetch, YT search resolves each track) |
| Lyrics | ``lyrics`` (no args; ``LyricsResolver`` cascade hits LRCLIB synced → LRCLIB plain → None; returns truncated body inline in the ``[SILENT]`` reply) |

``queue_many`` takes a ``tracks: list[str]`` for multi-song requests in one utterance; see [[music-queue-many-tool]]. ``apply_effect`` takes an ``effect`` name from the registry; see [[music-filter-presets]]. Move / remove / clear take 1-based positions to match the user-facing ``format_queue()`` display; see [[music-queue-primitives]]. ``autoplay`` toggles continuous playback via the cascade in [[music-autoplay-cascade]].

## Autoplay (queue-empty extension)

When the queue drains and ``GuildMusicPlayer.autoplay_enabled`` is ``True``, the player loop calls ``_try_autoplay_inject()`` before breaking. The helper:

1. Reads the last-played track (``_last_played_track``) — captured just before each FFmpeg respawn loop so it survives ``set_effect`` / ``replay`` / ``previous`` without resetting.
2. Lazily constructs an ``AutoplayEngine`` ([music/autoplay.py](../../src/poob/music/autoplay.py)) bound to the shared ``AsyncYTDL`` and a fresh-read view of ``queue.history``.
3. Awaits ``engine.get_next(seed)``, which cascades ``ytmusicapi.get_watch_playlist`` → yt-dlp on the ``RD<videoId>`` mix URL → random non-seed history shuffle. Every tier's exception is caught and logged; the engine always returns ``Track | None``.
4. On a non-None result, ``queue.add(track)`` enqueues it and ``queue.get_next()`` rotates it into ``current``.
5. On ``None``, the loop breaks with the existing ``"Queue empty, player loop ending"`` log line.

Per-guild state lives on the player instance (no SQLite persistence yet, by design — matches the ``loop_mode`` / ``shuffle`` / ``volume`` pattern). Resets on bot restart. See [[music-autoplay-cascade]] for the cascade design + deferred items (Last.fm tier, LLM-as-DJ "vibe" mode, persistence).

## Named playlists (per-guild SQLite persistence)

Separate from runtime player state, the cog persists user-defined named track collections per guild via ``GuildPlaylistsRepository`` ([storage/repositories/playlist_repo.py](../../src/poob/storage/repositories/playlist_repo.py)) and the ``guild_playlists`` table (added in the [storage/database.py](../../src/poob/storage/database.py) bootstrap). Four brain actions — ``save_playlist`` / ``load_playlist`` / ``list_playlists`` / ``delete_playlist`` — let voice or text users save the current queue under a name and recall it later. Case-insensitive uniqueness per guild; cross-guild isolation guaranteed by the ``guild_id`` partition. Stream URLs are NOT persisted (they expire ~6 h on YouTube); the player's existing resolve-at-play path rebuilds them. See [[music-named-playlists]] for the full design + alternatives.

## Key files

- [music/queue.py](../../src/poob/music/queue.py) — Track dataclass, MusicQueue with loop / shuffle / move / previous
- [music/effects.py](../../src/poob/music/effects.py) — Effect preset registry + ``resolve_effect_chain``
- [music/ytdl.py](../../src/poob/music/ytdl.py) — AsyncYTDL wrapper + pre-download
- [music/autoplay.py](../../src/poob/music/autoplay.py) — ``AutoplayEngine`` cascade (ytmusicapi → ytdl mix URL → history shuffle)
- [music/spotify.py](../../src/poob/music/spotify.py) — ``SpotifyPlaylistResolver`` (public-playlist URL → ``[{title, artist}]``)
- [music/lyrics.py](../../src/poob/music/lyrics.py) — ``LyricsResolver`` + ``ParsedLyrics`` (LRCLIB synced → plain cascade; position-aware accessors)
- [storage/repositories/playlist_repo.py](../../src/poob/storage/repositories/playlist_repo.py) — ``GuildPlaylistsRepository`` (per-guild named-playlist persistence)
- [music/player.py](../../src/poob/music/player.py) — MixingAudioSource + BufferedAudioSource + GuildMusicPlayer (position tracker + respawn loop + replay / previous / set_effect + autoplay queue-empty hook)
- [discord_bot/cogs/music_cog.py](../../src/poob/discord_bot/cogs/music_cog.py) — handler entrypoint (action dispatch)

## Invariants

- **One source per voice client.** MixingAudioSource is the sole source for the duration of a VC session.
- **Temp files must be cleaned up.** `download_track()` creates temp files; `cleanup_track_file()` must run in player loop / stop / destroy paths. Skipping cleanup causes disk exhaustion.
- **`is_playing` is a property, not a method.** See [[pycord-is-playing-is-a-property]].
- **Grace period on overlay** — cloud TTS buffers; first few `read()` returns empty. Without the grace period, the mixer kills the overlay before audio starts.
- **Silence vs empty bytes** — returning `b""` from `read()` tells Pycord the track ended. On buffer underrun, return silence (`b"\x00" * 3840`) to keep the stream alive while the buffer refills.
