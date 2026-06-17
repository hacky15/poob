---
type: incident
status: resolved
date: 2026-06-17
tags: [voice, discord, music, autojoin, voice-client]
related: [[voice-architecture]] [[music-player-architecture]] [[deepgram-streams-leak-on-cleanup]] [[one-handler-discord]]
---

# Poob couldn't join ANY voice channel — stale "zombie" voice client blocked auto-join

## Symptom

Operator (2026-06-17): "Poob is not joining VCs." Every music-triggered join
failed; users got *"you gotta be in a voice channel for me to play anything."*

Logs (voice.log / brain.poob):

```
00:48:52  poob.tool_route action=play query='bad girlfriend...'
00:48:52  [ERROR] discord.music_cog Auto-join failed error='Already connected to a voice channel.' guild=702353477602377769
00:48:52  music.response "you gotta be in a voice channel for me to play anything. hop in and try again."
   (repeated 00:49, 00:59 — every attempt)
```

## Root cause

A **stale / half-dead `guild.voice_client`**. At `00:13:37` a session was
cleaned up and Discord's voice client **reconnected instead of staying down**
(`handshake terminated` → `Connecting to voice... handshake complete`),
leaving `guild.voice_client` in a zombie state: `is_connected()` returns
**False**, but Discord's gateway still holds the voice connection.

`MusicCog._auto_join_requester_vc` then called `channel.connect(timeout=15.0)`
**with no guard**. The caller reaches auto-join precisely when
`guild.voice_client` is falsy *or* `not is_connected()` (music_cog.py:316-319) —
so with the zombie, auto-join fired, but `channel.connect()` raised **"Already
connected to a voice channel"** because Discord's gateway disagreed with
`is_connected()`. Caught → "Auto-join failed" → return None → the "hop in"
message.

**Asymmetry that hid it:** `/join` survived because `VoiceCog.join_voice`
force-disconnects first (`_force_disconnect` → `guild.voice_client.disconnect(
force=True)`, voice_cog.py:94-98). The music auto-join path never did.

Zombie *creation* (secondary): Discord's voice client auto-reconnects on a
dropped connection (seen repeatedly: `code=1006 → Reconnecting`). When a
session is torn down but the reconnect fires, a session-less voice client
lingers. This is Discord-voice-client behavior, not specific to our code; the
fix below makes auto-join resilient to it regardless of how a zombie forms.

## Fix

**Immediate:** restarted the bot to clear the in-memory zombie (the only lever
without a deploy). Poob joined again instantly.

**Robust (source):** `_auto_join_requester_vc` now force-disconnects any
lingering `guild.voice_client` before `channel.connect()` — mirroring `/join`:

```python
if guild.voice_client is not None:
    try:
        await guild.voice_client.disconnect(force=True)
    except Exception:
        pass
vc = await channel.connect(timeout=15.0)
```

The caller only reaches auto-join when we're not validly in a VC, so any
existing client is stale — force-disconnecting it is always correct, and it
clears Discord's gateway state so `connect()` succeeds. Now a zombie can never
block auto-join; the bot self-heals on the next join instead of needing a
restart. [music_cog.py](../../src/poob/discord_bot/cogs/music_cog.py)

## Validation

`tests/unit/test_music_autojoin_stale_vc.py`: half-dead client →
`disconnect(force=True)` then `connect` (the prod zombie); clean slate connects
directly; requester-not-in-VC still returns None; grep-as-test locks the guard.
Full unit suite green.

## Follow-up

The zombie-creation path (cleanup → voice-client auto-reconnect leaving a
session-less client) is worth a closer look — ensure session teardown issues a
clean `disconnect(force=True)` so the reconnect loop doesn't fire. Lower
priority now that auto-join self-heals. Related teardown work:
[[deepgram-streams-leak-on-cleanup]].
