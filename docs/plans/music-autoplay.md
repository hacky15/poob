---
type: plan
status: active
date: 2026-05-20
tags: [music, voice, brain, ytdl, ytmusicapi, autoplay]
related: [[music-player-architecture]] [[music-bot-feature-roadmap]] [[music-queue-primitives]] [[one-handler-music-contract]]
---

# Music autoplay — keep playing when the queue empties

## Goal

When the queue drains and the user has autoplay enabled for the guild, the player keeps going by enqueueing a "next track" derived from the last-played seed. Mirrors what FredBoat/Hydra/Aiode ship, with the twist that we expose it as a brain-tool action so voice commands like "Poob, turn on autoplay" route through `music_assistant` without any new slash command.

The free-tier autoplay item from [[music-bot-feature-roadmap]] §6. Spotify recs are gated since Feb 2026 — out of scope. We use YouTube Music's recommender via `ytmusicapi` as the primary, with yt-dlp on YT's `RD<videoId>` mix URL as the fallback, and last-played-history shuffle as the last-resort.

## Cascade design

Three tiers, fail-forward:

1. **ytmusicapi `get_watch_playlist(videoId=seed.identifier, limit=10)`** — best quality, sub-second latency, no auth. Uses YT Music's `RDAMVM<videoId>` mix prefix internally; matches what Hydra/Vexera-style bots ship. Returns track titles + video IDs which we resolve via `AsyncYTDL.search(f"https://youtu.be/{vid}")` to a full `Track` object.
2. **yt-dlp on `https://www.youtube.com/watch?v=<id>&list=RD<id>`** — plain-YouTube mix URL. `AsyncYTDL.extract_info(url)` returns a playlist info dict; we pick a not-recently-played entry from `info["entries"]`. Slower (~1-3 s) but doesn't depend on ytmusicapi's unofficial API.
3. **`history_shuffle(seed)`** — pull a random track from the last-100 queue history that isn't the seed itself. Last-resort fallback; useful when both YouTube paths fail (network issue, video deleted, etc.) so playback doesn't dead-end.

Each tier's failure is logged at WARN level with the exception trimmed to 120 chars — same idiom as the existing VLM cascade and `ytdl.search` widening fallback. No tier is allowed to raise out of `AutoplayEngine.get_next()`; the function always returns `Track | None`.

## Brain tool surface

Extend `MUSIC_TOOL` in [src/poob/brain/poob.py](../../src/poob/brain/poob.py):

- Add `"autoplay"` to the `action` enum.
- Add a new `mode` arg: `string`, enum `["on", "off", "status"]`, only consumed by `action="autoplay"`.
- Update the action's description so the LLM knows: "Toggle continuous playback when queue empties. mode='on' enables, 'off' disables, 'status' reports current state."

This matches the established pattern (`apply_effect` uses `effect`, `volume` uses `value`). One new arg, scoped to one action.

## Player integration

In [src/poob/music/player.py](../../src/poob/music/player.py:807-812), the queue-empty branch of the player loop currently breaks unconditionally:

```python
track = self.queue.get_next() if self.queue.current is None else self.queue.current
if track is None:
    track = self.queue.get_next()
if track is None:
    log.info("Queue empty, player loop ending")
    break
```

Extend the second `if track is None:` branch so it consults the autoplay engine first:

```python
if track is None:
    if self.autoplay_enabled:
        seed = self._last_played_track or (self.queue.history[-1] if self.queue.history else None)
        try:
            generated = await self.autoplay.get_next(seed)
        except Exception as exc:
            log.warning("autoplay engine raised", error=str(exc)[:120])
            generated = None
        if generated is not None:
            log.info("autoplay enqueued", title=generated.title[:60])
            self.queue.add(generated)
            track = self.queue.get_next()
    if track is None:
        log.info("Queue empty, player loop ending")
        break
```

Why a seed-track parameter and not just "the last thing in history"? Two reasons: (a) `queue.current` is `None` at the moment we hit this branch (`get_next()` set it None when the queue emptied), and (b) we want the LAST track that ACTUALLY played, not e.g. a track that was added-then-skipped without playing. The player already tracks this implicitly via `queue.history`, but to be defensive we keep our own `_last_played_track` attribute updated in the play-loop just before we hand off to the FFmpeg respawn loop.

## Per-guild state

Runtime-only attribute on `GuildMusicPlayer` (matches the existing pattern for `loop_mode`, `shuffle`, `volume` — all reset on bot restart). No persistence in v1. If usage shows the toggle needs to survive restarts, add a guild-prefs SQLite table in a follow-up — see "deferred" below.

- `GuildMusicPlayer.autoplay_enabled: bool` (default `False`).
- Setter via `GuildMusicPlayer.set_autoplay(mode: AutoplayMode)` where `AutoplayMode` is `Enum{ON, OFF}`. The brain's `mode="status"` doesn't mutate — the dispatcher reads `player.autoplay_enabled` directly.

## Music cog dispatch

Add an `if action == "autoplay":` block in [src/poob/discord_bot/cogs/music_cog.py](../../src/poob/discord_bot/cogs/music_cog.py) following the same response shape as `apply_effect`:

```python
if action == "autoplay":
    mode_str = (mode or "").strip().lower()
    if mode_str not in {"on", "off", "status"}:
        return "[SILENT]Autoplay mode? (on, off, status)"
    if mode_str == "status":
        state = "on" if player.autoplay_enabled else "off"
        return f"[SILENT]Autoplay is {state}."
    player.autoplay_enabled = (mode_str == "on")
    return f"[SILENT]Autoplay {'enabled' if player.autoplay_enabled else 'disabled'}."
```

## Files to change

- [src/poob/music/autoplay.py](../../src/poob/music/autoplay.py) — NEW. `AutoplayEngine` with the three-tier cascade. Async, takes `AsyncYTDL` + a queue-history accessor at init.
- [src/poob/music/player.py](../../src/poob/music/player.py) — extend the queue-empty branch; track `_last_played_track`; hold an `AutoplayEngine` instance; expose `autoplay_enabled`.
- [src/poob/discord_bot/cogs/music_cog.py](../../src/poob/discord_bot/cogs/music_cog.py) — dispatch `action="autoplay"` with `mode` arg.
- [src/poob/brain/poob.py](../../src/poob/brain/poob.py) — add `"autoplay"` to the action enum + `mode` arg description.
- [pyproject.toml](../../pyproject.toml) — add `ytmusicapi>=1.10.0` to runtime deps.
- [tests/unit/test_autoplay_engine.py](../../tests/unit/test_autoplay_engine.py) — NEW. Tier-by-tier cascade tests.
- [tests/unit/test_music_player_autoplay.py](../../tests/unit/test_music_player_autoplay.py) — NEW. Queue-empty hook tests.
- [tests/unit/test_music_handler_actions.py](../../tests/unit/test_music_handler_actions.py) — extend with `test_autoplay_*` cases (on/off/status/missing-mode).

## TDD list

In TDD order — write each test, watch it fail, then minimal implementation to make it pass.

### `test_autoplay_engine.py`

1. `test_no_seed_returns_none` — `get_next(None)` returns None without touching any tier.
2. `test_ytmusic_tier_returns_track` — mock `ytmusicapi.get_watch_playlist` to return one candidate; mock `ytdl.search` to return a `Track`; assert engine returns that track.
3. `test_ytmusic_tier_skips_seed` — first candidate equals seed.identifier; engine picks the second.
4. `test_ytmusic_failure_falls_to_ytdl_mix` — ytmusicapi raises; engine calls `ytdl.extract_info` on the mix URL; returns the first viable entry.
5. `test_ytdl_mix_skips_recently_played` — mix URL returns 5 entries; engine skips the ones already in `queue.history`.
6. `test_both_tiers_fail_falls_to_history_shuffle` — both ytmusic + ytdl raise; engine returns a random non-seed track from history.
7. `test_all_tiers_fail_returns_none` — all three fail (history is also empty); engine returns None (caller breaks the loop).
8. `test_engine_swallows_all_exceptions` — any tier raising propagates as None, not a raise.

### `test_music_player_autoplay.py`

9. `test_queue_empty_with_autoplay_off_breaks_loop` — autoplay disabled; the existing queue-empty break path is unchanged.
10. `test_queue_empty_with_autoplay_on_enqueues_and_continues` — autoplay enabled; engine returns a track; queue.add called; player loop continues.
11. `test_queue_empty_with_autoplay_on_but_engine_returns_none_breaks` — autoplay enabled but cascade exhausted; loop still breaks (no infinite loop).
12. `test_autoplay_seed_is_last_played_not_None` — verify the engine is called with the last-played track as seed, not None.

### `test_music_handler_actions.py` additions

13. `test_autoplay_on_enables_and_returns_silent_confirmation`.
14. `test_autoplay_off_disables_and_returns_silent_confirmation`.
15. `test_autoplay_status_reports_current_state`.
16. `test_autoplay_missing_mode_returns_help`.

## Out of scope (deferred to a v2 of autoplay if usage justifies)

- **LLM-as-DJ "vibe" mode** — feed last 5 tracks to a chat model, ask for next title. Cool but expensive per-call and slow. Skip until a user explicitly asks.
- **Last.fm `track.getSimilar` tier** — adds an API key (free but one more secret) and a title-resolution step. Defer unless ytmusicapi fragility hurts. The vault has guidance in [[music-bot-feature-roadmap]].
- **Per-guild persistence** — store autoplay state in SQLite so it survives restarts. Add when we have a guild-prefs table to put it in; for now it's a runtime attribute alongside loop/shuffle.
- **Now-playing embed toggle button** — the buttons set in [[music-now-playing-embed-buttons]] is full; adding another would require resizing or paginating the view. Defer until we decide the button taxonomy.
- **Autoplay-source preference** — let a user pin `autoplay_source=ytmusic` to bypass the cascade. Premature until cascade fragility shows.

## Acceptance

- ✅ All 16 new tests pass; full unit suite passes (no regressions to the existing 1000+ tests).
- ✅ Manual smoke: in a real voice channel with the bot, `play <song>`, wait for queue to drain, observe a related track auto-enqueues and plays.
- ✅ Voice command "Poob, turn on autoplay" triggers `music_assistant(action=autoplay, mode=on)` via the brain.
- ✅ Decision note + architecture note updated when shipping.
