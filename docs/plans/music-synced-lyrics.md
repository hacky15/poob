---
type: plan
status: active
date: 2026-05-23
tags: [music, voice, brain, lyrics, lrclib, discord-ui]
related: [[music-player-architecture]] [[music-bot-feature-roadmap]] [[music-now-playing-embed-buttons]] [[music-named-playlists]] [[music-spotify-playlist-import]]
---

# Synced lyrics overlay — LRCLIB + live embed tick

## Goal

The visible "wow" feature from [[music-bot-feature-roadmap]] §10. Voice or text user says *"Poob, show me the lyrics"* → bot fetches synced lyrics (LRC format) → posts an embed in the text channel with the current line highlighted → ticks every ~1.5 s, editing the embed in place so the current line tracks the music. Falls back to plain-text lyrics in a single non-ticking embed when synced lyrics aren't available for the track.

Free APIs only (LRCLIB, no auth). No paid tier. Matches what Uzox / Aiode ship and what Mee6 gates behind premium — same "free what others charge for" differentiation we did with autoplay.

## Cascade

The `syncedlyrics` PyPI package aggregates over LRCLIB / Musixmatch / Netease. LRCLIB is auth-free; the others require keys we don't want to provision. So in practice the cascade is short:

1. **LRCLIB synced (preferred)** — `syncedlyrics.search(f"{title} {artist}", providers=["Lrclib"])` returns LRC-format string when present. Parse to `[(timestamp_seconds, text), …]` tuples.
2. **LRCLIB plain (fallback)** — same call with `plain_only=True` parameter; returns unsynced text when the synced version isn't catalogued.
3. **None** — soft-error to user with `[SILENT]No lyrics found for "<title>".`

Per-tier exceptions caught + logged at WARN, never propagated. Same fail-forward shape as [[music-autoplay-cascade]] and the [[music-spotify-playlist-import]] resolver.

## Module

[src/poob/music/lyrics.py](../../src/poob/music/lyrics.py) — two exports:

```python
@dataclass
class ParsedLyrics:
    title: str
    artist: str
    is_synced: bool                   # True when timestamps are present
    lines: list[tuple[float, str]]    # (timestamp_seconds, text); single (0.0, full_text) for plain
    
    def current_line_at(self, position_seconds: float) -> str | None:
        """Pick the last line whose timestamp <= position. None if before first line."""
    
    def upcoming_window(
        self,
        position_seconds: float,
        before: int = 1,
        after: int = 3,
    ) -> list[tuple[bool, str]]:
        """Window for the live embed: (is_current, text) for `before` lines back + the
        current line + `after` lines ahead. Used so the embed shows context, not just the
        single current line."""


class LyricsResolver:
    async def fetch(self, title: str, artist: str | None = None) -> ParsedLyrics | None:
        ...
```

Internals: `syncedlyrics` is synchronous; calls run under `asyncio.to_thread`. The LRC parser is regex-based — `[mm:ss.xx]text` — and resilient to malformed lines (skip with WARN, don't raise). Lazy-import `syncedlyrics` so test runs don't require the package installed.

## Live overlay tick

The overlay UX is a background `asyncio.Task` per active `lyrics` invocation. The task:

1. Posts the initial embed showing the current LRC window.
2. Loops every 1.5 s: read `player.position_seconds`, recompute the window, edit the embed.
3. Exits when any of: the track changes (player.current_track identity shift), the player stops, the user clicks the embed's Stop button, 30 min hard timeout.

Discord rate limit: 5 edits per 5 s per channel. At 1.5 s per tick we run at ~0.67 ops/s — well under the cap. We add `asyncio.sleep(1.5)` per loop with no jitter; if Discord 429s anyway, the edit call catches + sleeps an extra 3 s + retries.

The overlay class lives in [src/poob/discord_bot/views/lyrics_overlay.py](../../src/poob/discord_bot/views/lyrics_overlay.py) alongside the existing now-playing view from [[music-now-playing-embed-buttons]]. Mirrors that pattern: a `discord.ui.View` subclass with a Stop button, a `asyncio.Task` for the tick loop, and a `_render_embed()` method that builds the current frame.

Per-guild concurrency: at most one overlay active per guild. Starting a new one cancels the previous task and edits the old embed to a "(overlay stopped — newer lyrics request)" footer.

## Brain tool surface

One new action on `music_assistant`:

| Action | Args | Behavior |
|---|---|---|
| `lyrics` | _none_ | Looks at the current track; fetches lyrics; starts the live overlay if synced lyrics found, posts a plain-text fallback embed otherwise; replies with `[SILENT]Lyrics for X by Y.` |

(No `lyrics_stop` action — the embed's Stop button is the cancellation surface, mirroring how the now-playing embed handles `previous` / `replay` / `leave` from [[music-now-playing-embed-buttons]].)

## Music cog dispatch

In [music_cog.py](../../src/poob/discord_bot/cogs/music_cog.py), one new `if action == "lyrics":` block. Wires the resolver into `__init__` (optional, `None` disables the action with a soft error). Soft-error matrix:

- Resolver unconfigured / disabled → `[SILENT]Lyrics aren't configured on this stack yet.`
- No current track → `[SILENT]Nothing is playing — can't show lyrics for nothing.`
- Resolver returns None → `[SILENT]No lyrics found for "<title>".`
- Synced lyrics found → start the overlay; reply `[SILENT]Showing lyrics for <title>.`
- Plain-text only → post the single embed; reply `[SILENT]Plain lyrics for <title> (no synced version found).`

The brain narrates around the `[SILENT]` reply; the actual lyrics live in the channel embed, not in Poob's spoken response (TTS reading 200 lines of lyrics aloud would be unhinged).

## TDD list

### `tests/unit/test_lyrics_resolver.py` — 11 tests

1. `test_parse_lrc_single_line` — `[00:12.34]Hello world` → `[(12.34, "Hello world")]`.
2. `test_parse_lrc_multiple_lines_sorted` — `[00:10]A\n[00:20]B\n[00:05]C` → sorted by timestamp.
3. `test_parse_lrc_handles_mm_ss_only` — `[01:23]X` → `[(83.0, "X")]`.
4. `test_parse_lrc_skips_malformed_lines` — mixes valid + malformed; only valid lines kept.
5. `test_parsed_lyrics_current_line_at_picks_last_passed_timestamp` — at position 15 with lines at 10/20/30, picks the 10 line.
6. `test_parsed_lyrics_current_line_at_before_first_returns_none` — position 5, first line at 10 → None.
7. `test_parsed_lyrics_upcoming_window_returns_context` — window of `before=1, after=3` shape.
8. `test_resolver_returns_synced_when_lrclib_has_synced` — mocked syncedlyrics returns LRC; `is_synced=True`.
9. `test_resolver_falls_back_to_plain` — synced returns None, plain returns text; `is_synced=False`, single `(0.0, text)` line.
10. `test_resolver_returns_none_when_no_lyrics_anywhere` — both calls return None.
11. `test_resolver_swallows_exceptions_returns_none` — `syncedlyrics` raises; resolver returns None.

### `tests/unit/test_music_handler_actions.py` additions — 4 tests

12. `test_lyrics_no_current_track_returns_silent_error` — `player.current_track=None`.
13. `test_lyrics_resolver_not_configured_returns_soft_error` — `cog._lyrics_resolver=None`.
14. `test_lyrics_synced_found_replies_and_would_start_overlay` — resolver returns ParsedLyrics(is_synced=True); reply mentions the title.
15. `test_lyrics_only_plain_replies_with_plain_note` — resolver returns ParsedLyrics(is_synced=False); reply mentions "plain" or "no synced".

(Overlay-task lifecycle is integration territory — covered by manual smoke after ship, not unit-tested here.)

## Files to change

- [pyproject.toml](../../pyproject.toml) — add `syncedlyrics>=0.10.0`.
- [src/poob/music/lyrics.py](../../src/poob/music/lyrics.py) — NEW. `ParsedLyrics` + `LyricsResolver`.
- [src/poob/discord_bot/views/lyrics_overlay.py](../../src/poob/discord_bot/views/lyrics_overlay.py) — NEW. `LyricsOverlay` view + tick task. (Optional file — can land as part of `music_cog.py` if a stand-alone view file feels heavy for v1.)
- [src/poob/discord_bot/cogs/music_cog.py](../../src/poob/discord_bot/cogs/music_cog.py) — wire resolver into `__init__`, add `lyrics` dispatch block, manage per-guild active-overlay map.
- [src/poob/discord_bot/bot.py](../../src/poob/discord_bot/bot.py) + [src/poob/main.py](../../src/poob/main.py) — instantiate resolver, pass into `MusicCog`.
- [src/poob/brain/poob.py](../../src/poob/brain/poob.py) — add `lyrics` to action enum + tool description.
- [tests/unit/test_lyrics_resolver.py](../../tests/unit/test_lyrics_resolver.py) — NEW, 11 tests.
- [tests/unit/test_music_handler_actions.py](../../tests/unit/test_music_handler_actions.py) — extend with 4 dispatch tests.

## Acceptance

- ✅ 15 new tests pass; full unit suite stays green (1126+ baseline).
- ✅ Manual smoke: play a popular song with synced lyrics on LRCLIB → "Poob lyrics" → embed posts, ticks every ~1.5 s in sync with the song.
- ✅ Manual smoke: play a track LRCLIB doesn't have → reply says "plain" or "no synced version" + posts plain text.
- ✅ Decision note shipped; this plan archives to "Superseded / historical".

## v1 scope adjustment (2026-05-23 — same day as the plan)

The originally-planned **live overlay tick loop** is deferred to a v2. Reason: posting + editing an embed in the text channel needs a channel reference inside `handle_music_request`, which currently only accepts `(message_text, user_id, guild_id, tool_args)` and the brain narrates `[SILENT]` replies into whatever channel the LLM was invoked from. Plumbing a channel through would touch every action's call site — a big lift for one feature.

v1 ships the resolver + on-demand fetch with the formatted lyrics returned inline in the `[SILENT]` reply (truncated to ~1800 chars to stay under Discord's 2000-char limit). The brain narrates only the announcement (`Showing lyrics for X by Y`); the actual lyrics body comes back as the `[SILENT]` block which Discord renders without TTS reading it aloud.

The live overlay still ships in v2 once the channel reference is wired through `handle_music_request`. The resolver + parser + `current_line_at` / `upcoming_window` machinery are already there waiting to be driven — no rework needed.

## Deferred (v2 candidates)

- **Live overlay tick loop** — see above. Channel-reference plumbing through the music handler is the unblock.
- **Pin lyrics to the now-playing embed** as an inline lyrics field. Requires the now-playing view to accept a lyrics-state field and the overlay tick to edit that embed instead of a separate one. Add if users say "the second embed is ugly."
- **Per-line karaoke highlighting** (bolding the current word, not the whole line). Requires word-level timestamps which LRCLIB doesn't provide; would need a different format like KRC. Defer indefinitely.
- **Pagination** for >1800-char lyrics. v1 truncates with a "(truncated)" footer; v2 could send multiple messages or a paginated embed view.
- **Translation** (lyrics in another language). LRCLIB has some; deferred until requested.
