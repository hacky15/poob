---
type: incident
status: resolved
date: 2026-05-09
tags: [voice, music, dual-pipeline, listening, auto-join]
related: [[voice-architecture]] [[music-player-architecture]] [[one-handler-discord]]
---

# Auto-join played music but skipped wake-word + STT — bot was deaf in voice

## Symptom

User in a newly-joined Discord server posted a YouTube link in a text channel: `play https://youtube.com/watch?v=...`. Poob auto-joined the user's voice channel and started streaming the song. User then said `"Hey Poob, max volume"` in the VC. **No response.** Confirmed in prod logs: zero `Utterance complete` / `Dual: wake word addressed` events for that guild after the auto-join.

## Root cause

`MusicCog._auto_join_requester_vc` ([music_cog.py:116](../../src/poob/discord_bot/cogs/music_cog.py#L116)) was a deliberately *lightweight* join — it called `channel.connect()` and returned the `VoiceClient`, full stop. The docstring said:

> Lightweight join — no VoiceSession (STT/recording). For full voice interaction the user invokes /join. The music player only needs a connected VoiceClient to stream audio.

The intent was privacy-conservative: only listen when the user explicitly invokes `/join`. In practice the assumption was wrong — once Poob is in voice playing music, users naturally reach for voice commands ("skip", "louder", "max volume") and expect them to work. The "lightweight" handle was a footgun: the bot connected, played audio, looked alive, but had no recording sink, no `RealtimeAudioSink`, no dual pipeline. Saying "Hey Poob X" went into the void with zero log evidence — even harder to diagnose because the bot *appeared* fine.

`/join` did the right setup inline (connect + DAVE wait + `start_recording` with the sink + horniness roll + entrance), but that whole block was hard-wired into the slash command handler. There was no shared path the music cog could call.

## Fix

Two changes in one commit:

### 1. Extract `VoiceCog.setup_session_for_vc(vc, channel, is_stage, play_entrance)`

All the post-`channel.connect()` logic from `/join` moved into this method on VoiceCog: stage promotion, `_session_factory(vc)`, register in `self._sessions`, DAVE handshake wait (15s, proceed regardless per [[dave-timeout-fail-hard-regression]]), `RealtimeAudioSink` build with the right stale-buffer detection variant, `vc.start_recording(...)`, horniness roll. Returns the `VoiceSession` or `None` on error.

`/join` now calls this helper after its own `channel.connect()` and stage-inviter promotion. Body shrank ~80 lines without behavior change. The entrance catchphrase is gated behind `play_entrance: bool` so non-`/join` callers can suppress the intro (auto-join skips it; the user wants music, not "what's up").

### 2. Hand-off from MusicCog to VoiceCog

`bot.py` now passes a `_setup_voice_session` async callable to MusicCog (alongside the existing `_get_voice_session`). The callable wraps `voice_cog.setup_session_for_vc(...)`. Keeps the dependency one-way: `bot.py` knows both cogs; MusicCog only sees callables; VoiceCog doesn't know MusicCog exists.

`MusicCog._auto_join_requester_vc` calls the new callable after its `channel.connect()` succeeds. Failure of the listening setup logs a warning but doesn't fail the music play — music still streams; the bot just doesn't hear voice commands. That's a strict improvement over the old "no session ever" behavior.

## Bonus fix in same commit — "max volume" tool routing

Once the bot can hear, the LLM still has to map "max volume" → numeric value. The `MUSIC_TOOL` schema's `value` description was *"Only required when the user specifies a number"*, with no guidance on named extremes. Tightened to enumerate the common cases: `max`/`crank`/`loudest` → 200, `mute`/`min` → 0, `half` → 100, `low`/`quiet` → 50, `high`/`loud` → 150. The action description also clarifies that absolute targets ("max", "mute", a percentage) route to `volume`, while relative changes ("turn it down", "louder") go to `volume_up`/`volume_down`.

The handler at [music_cog.py:319](../../src/poob/discord_bot/cogs/music_cog.py#L319) already accepts 0-200 (clamped to `min(2.0, value/100.0)` for player gain), so no handler changes needed.

## Validation

- `python -m pytest tests/unit/` — all green, no regressions on the brain or voice surface.
- Subjective in-prod check: text-channel `play <url>` in a server where Poob isn't already in voice. Bot auto-joins, music starts, then `"Hey Poob, max volume"` should now route through wake-word → STT → `music_assistant(action=volume, value=200)` → music goes loud.
- Log signals to watch:
  - `Auto-joined requester's voice channel` (existing)
  - **`Auto-join wired listening pipeline`** (new) — confirms STT/wake came up post-connect
  - `Recording started with RealtimeAudioSink guild=<id>` — sink active for the auto-joined guild
  - `Dual: wake word addressed` — first wake event in that guild after auto-join

## Why this didn't surface earlier

Until recently Poob was only in one server, where the user had a habit of invoking `/join` first (DAVE handshake friction made `/join` the canonical entry). Auto-join was a fallback path that rarely got exercised in isolation. After the multi-guild work landed and Poob got invited to a second server, music-link-first interactions surfaced the missing listening setup immediately.

## Follow-ups

- The volume-tool prompt tightening is provisional — if `max` reliably routes to `volume action=volume_up` instead of `volume value=200`, expand the action description further or add an explicit example.
- If the privacy concern about listening-by-default ever resurfaces, add a per-guild opt-out via env var or admin command rather than reverting the auto-join setup.
