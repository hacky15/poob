---
type: decision
status: active
date: 2026-05-23
tags: [music, voice, brain, lyrics, lrclib]
related: [[music-player-architecture]] [[music-synced-lyrics]] [[music-spotify-playlist-import]] [[music-named-playlists]] [[music-bot-feature-roadmap]] [[music-now-playing-embed-buttons]]
---

# Synced lyrics — LRCLIB cascade, inline `[SILENT]` reply (live overlay deferred to v2)

## Context

[[music-bot-feature-roadmap]] section §10 — "synced lyrics overlay" — is the final feature in the music ship order. The roadmap framed it as the visible "wow": a periodically-updated embed showing the current LRC line as the song plays.

LRCLIB ships as a free, auth-free aggregator over LRC files (the synced-lyrics format with `[mm:ss.xx]text` entries). The `syncedlyrics` PyPI package wraps LRCLIB + Musixmatch + Netease; we use the LRCLIB tier (no key needed) and skip the others. Plain-text lyrics from LRCLIB are the fallback when synced lyrics aren't catalogued for a given track.

A v1 ship of the full overlay-tick UX hit a plumbing wall: `MusicCog.handle_music_request` receives `(message_text, user_id, guild_id, tool_args)` and returns a `[SILENT]…` reply string the brain narrates back. It has no channel reference, no way to post a fresh embed in arbitrary channels, no way to attach a background tick task to an existing message context. Threading a channel reference through every action's call site is a touched-everywhere refactor whose blast radius dwarfs the lyrics feature.

## Decision

**Ship v1 as inline `[SILENT]` reply with full lyrics body; defer the live overlay-tick loop to v2.** The resolver + LRC parser + position-aware accessors (`current_line_at`, `upcoming_window`) all land in v1 so v2 only needs to wire them to a tick task once a channel reference is available.

**Resolver design** ([src/poob/music/lyrics.py](../../src/poob/music/lyrics.py)):

- `LyricsResolver.fetch(title, artist)` returns `ParsedLyrics | None`.
- Cascade: LRCLIB synced → LRCLIB plain → None. Synced is preferred; plain is the fallback for catalog gaps.
- `syncedlyrics` lazy-imported (matches the `ytmusicapi` + `spotipy` pattern; module import is cheap, package only required when the resolver actually runs).
- Synchronous `syncedlyrics.search` runs under `asyncio.to_thread`. Every call is `try/except`'d and returns `None` on failure with a WARN log — fail-forward to the next tier; never raise out of `fetch`.

**ParsedLyrics methods**:

- `current_line_at(position_seconds)` — picks the last line whose timestamp ≤ position; returns `None` before the first line (pre-vocal intro).
- `upcoming_window(position, before=1, after=3)` — returns `[(is_current, text), ...]` for the embed renderer; v1 doesn't use it, v2 will.

**Brain action surface**:

| Action | Args | Behavior |
|---|---|---|
| `lyrics` | _none_ | Looks at the current track. Splits title heuristically on `" - "` (YouTube's most common "Song - Artist" pattern). Fetches via the resolver. Returns the lyrics body inline in the `[SILENT]…` reply (truncated at 1800 chars to stay under Discord's 2000-char message limit; appends "(truncated)" footer when cut). For plain-text, the reply tags `(plain — no synced version available)`. |

Soft-error matrix matches the autoplay / Spotify / playlist patterns:
- Resolver `None` → `[SILENT]Lyrics aren't configured on this stack yet.`
- No current track → `[SILENT]Nothing is playing — can't show lyrics for nothing.`
- Resolver returned `None` → `[SILENT]No lyrics found for '<title>'.`

**No live overlay in v1**. The roadmap "wow" UX of the periodically-edited embed is queued in [[music-synced-lyrics]] as a v2 follow-up. Channel reference plumbing through the music handler is the unblock.

## Alternatives considered

- **Plumb the channel reference through `handle_music_request` now.** The right long-term move but the surface change touches every action's call sites (voice-cog, text-mention, button-callback). Out of scope for one feature; do it when a second action also needs the channel.
- **Hard-code a target channel from config** (e.g. `MUSIC_LYRICS_CHANNEL_ID`). Rejected: implies single-channel use which doesn't match the multi-guild / per-channel reality.
- **Reply with the lyrics as raw text and let the LLM narrate the FIRST line aloud** to simulate the "wow." Rejected: TTS reading lyrics out of sync with the music is uncanny, not delightful. The chat-side rendering is enough for v1; the live-sync UX is what unlocks the wow when v2 lands.
- **Use Musixmatch or Genius via paid keys** for higher catalog coverage. Rejected for now — LRCLIB's free tier covers the vast majority of popular tracks, and adding a paid key contradicts the "$0 spend by default" rule from the project conventions.
- **Render lyrics inside the now-playing embed** (a lyrics field on the existing view). Rejected: now-playing-embed real estate is already tight per [[music-now-playing-embed-buttons]]; adding lyrics would force pagination or eviction. Cleaner to keep lyrics as a separate `[SILENT]` body.

## Consequences

- ✅ One new brain action, one optional resolver wire-up. Zero existing surfaces broken; the soft-error path covers the "not configured" case gracefully.
- ✅ Resolver + ParsedLyrics already encode the position-aware accessors (`current_line_at`, `upcoming_window`) — when v2 ships the overlay-tick loop, it just calls those; no resolver rework needed.
- ✅ Composes with the other music features: lyrics show whatever's playing, regardless of whether it was queued via `play`, `queue_many`, `load_playlist`, `queue_spotify_playlist`, or autoplay.
- ⚠️ Title-artist heuristic (`split on " - "`) is approximate. YouTube titles like `"Bohemian Rhapsody — Official Music Video [Remastered] (4K)"` won't split cleanly; the resolver gets a noisy query string and LRCLIB may miss. Acceptable in v1; can refine in v2.
- ⚠️ Long lyrics (>1800 chars, common for hip-hop / verse-heavy tracks) truncate. Plain-text dump in a single message is suboptimal; pagination via multiple `[SILENT]` messages is queued as a v2 deferred item.
- ⚠️ LRCLIB is a community-driven catalog — niche tracks, very new releases, and non-Western music may have no entries. The plain-text fallback usually picks up the slack but not always. "No lyrics found" is a real outcome users will hit.

## Rollback

Single-commit revert. Removing the `lyrics` action from `MUSIC_TOOL`'s enum is a one-line gate that disables the action without touching code paths. The resolver stays in place harmlessly. New deps (`syncedlyrics`) sit unused in the image until the action is re-enabled.

If `syncedlyrics` itself breaks against LRCLIB or its API contract: lazy-import + try/except wrapper means the bot still boots; only the lyrics action soft-errors. No production crash; the user sees `[SILENT]No lyrics found for 'X'.` until a `syncedlyrics` upgrade restores function.

## References

- [[music-synced-lyrics]] — originating plan + v2 deferred-items list
- [[music-bot-feature-roadmap]] §10 — the ranking + LRCLIB / Musixmatch / Netease provider notes
- [src/poob/music/lyrics.py](../../src/poob/music/lyrics.py) — the resolver + ParsedLyrics
- [src/poob/discord_bot/cogs/music_cog.py](../../src/poob/discord_bot/cogs/music_cog.py) — `lyrics` action dispatch + the `_format_lyrics_for_reply` truncation helper
- [syncedlyrics on PyPI](https://pypi.org/project/syncedlyrics/) — upstream
- [LRCLIB](https://lrclib.net/) — the catalog itself
