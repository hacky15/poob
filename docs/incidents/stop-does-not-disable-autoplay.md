---
type: incident
status: resolved
date: 2026-06-02
tags: [music, autoplay, player, voice]
related: [[music-autoplay-cascade]] [[music-player-architecture]] [[music-text-requests-and-autoplay-followup]]
---

# "Stop" doesn't stop the music when autoplay is on (+ phantom "no music")

## Symptom

Operator, live: *"it's not stopping the autoplay — it's saying there's no music for some reason even though it's continuously playing music."* Repeated "stop music" / "stop playing music" / "clear music queue" commands (logged at 2026-06-02 00:35–00:36, all routed correctly to `music_assistant`) failed to actually stop playback, and some replies claimed nothing was playing.

## Root cause

`GuildMusicPlayer.stop()` ([player.py:733](../../src/poob/music/player.py)) cleared the queue and halted the current track but **never set `autoplay_enabled = False`**. With autoplay on, the sequence was:

1. `stop()` → `queue.clear_all()` + `voice_client.stop()`.
2. `voice_client.stop()` wakes the player loop (`_next_event`).
3. Loop: `queue.get_next()` → `None` (just cleared) → `_try_autoplay_inject()`.
4. `_try_autoplay_inject` sees `autoplay_enabled` **still True** + a seed (`_last_played_track`) → generates and enqueues a fresh track → playback resumes.

So "stop" was immediately undone by autoplay. The **"no music"** reply was the transient gap in that stop→refill race: a follow-up command checking `current_track` (= `queue.current`) caught it momentarily `None` (e.g. "Nothing is playing to skip") before autoplay refilled. During steady autoplay `current_track` is correctly set (autoplay calls `queue.get_next()`), so the empty state was only ever the race window.

## Fix

`GuildMusicPlayer.stop()` now sets `self.autoplay_enabled = False` first. Done at the player layer so it covers **every** stop path — the text/voice "stop" action, the now-playing embed's stop/leave buttons, and the `leave` action (all route through `player.stop()`). With autoplay off, the loop's `_try_autoplay_inject` returns `None`, the loop breaks, and stop actually stops — which also eliminates the refill race and the phantom "no music".

`clear` was intentionally left alone (clearing the upcoming queue with autoplay on is a different intent than stopping; no operator report against it — avoid speculative scope).

## Validation

- `tests/unit/test_music_player_autoplay.py::test_stop_disables_autoplay` — autoplay on → `stop()` → `autoplay_enabled is False`. Existing autoplay/handler tests stay green (62 in the two files).
- Full unit suite green.
- Post-deploy: with autoplay on, "stop" should halt playback and the voice log should show no `autoplay enqueued` after a stop until autoplay is re-enabled.

## Follow-ups

- Re-enabling is explicit ("autoplay on") — correct. If operators expect "stop" to remember autoplay and resume on the next "play", that's a separate product call; not assumed here.
- Related deferred item: compound "play X and autoplay" routing ([[music-text-requests-and-autoplay-followup]]).
