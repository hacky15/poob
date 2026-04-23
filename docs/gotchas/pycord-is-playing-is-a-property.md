---
type: gotcha
status: active
date: 2026-04-07
tags: [pycord, discord, music]
related: [[music-player-architecture]]
---

# `is_playing` is a property, not a method (Pycord)

## Trigger

Writing `player.is_playing()` on a `GuildMusicPlayer` or `VoiceClient`. Crashes with `'bool' object is not callable`.

## Why it happens

Pycord exposes `is_playing` as a property. Python evaluates the attribute access, returns a `bool`, and then the `()` tries to call the bool — TypeError.

## Don't

```python
if player.is_playing():   # BAD
    ...
```

## Do

```python
if player.is_playing:     # GOOD — property access
    ...
```

## Reference

- [music-player-architecture](../architecture/music-player-architecture.md)
- Also applies to `voice_client.is_playing` / `voice_client.is_paused` / `voice_client.is_connected` — all properties.
