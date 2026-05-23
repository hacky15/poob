---
type: plan
status: active
date: 2026-05-23
tags: [music, voice, brain, spotify, ytdl, cascade]
related: [[music-player-architecture]] [[music-bot-feature-roadmap]] [[music-named-playlists]] [[music-autoplay-cascade]] [[search-provider-cascade]]
---

# Spotify playlist URL import — `queue_spotify_playlist(url)` brain action

## Goal

Voice or text user pastes/says a Spotify playlist URL → bot resolves the track titles via Spotify's public-playlist API → searches each on YouTube via the existing `AsyncYTDL.search` → enqueues into the active guild player. Optionally, the resolved tracks can be saved as a named playlist via [[music-named-playlists]] for future recall without re-resolving.

This is item #8 from [[music-bot-feature-roadmap]]'s ship order. The roadmap §line-144 confirms Spotipy's `SpotifyClientCredentials` flow still works for read-only public-playlist `playlist_items` reads after the Feb 2026 dev-platform restrictions (those hit `/recommendations` and `/audio-features`, not basic playlist metadata).

## Scope

In-scope:
- Resolve a Spotify playlist URL → list of `{title, artist}` dicts
- Search each on YouTube; enqueue resolved tracks; report not-found count
- Soft-error when Spotify creds aren't configured (no crash, friendly message)
- Both web URL form (`https://open.spotify.com/playlist/<id>`) and URI form (`spotify:playlist:<id>`)

Out-of-scope (deferred):
- **Spotify track URLs** (single song). Could add a sibling `queue_spotify_track` action if asked.
- **Spotify album URLs**. Same shape but different endpoint; tackle if requested.
- **User-library auth** (OAuth flow for "my saved tracks"). Requires per-user OAuth + token store. Big lift; defer until usage demands.
- **Apple Music / Tidal / Deezer playlist URLs**. Same pattern but different vendor APIs each. Add adapters per-vendor as needed.

## Configuration

Two new env vars bound to two new `AppConfig` fields in [src/poob/config.py](../../src/poob/config.py):

```python
spotify_client_id: str = ""
spotify_client_secret: str = ""
```

Both default empty. When either is empty, the brain action soft-errors. Operator gets them from the Spotify Developer Dashboard (free, 5-minute setup); no Premium needed for public-playlist reads. Add to `.env.example` so it's discoverable.

## New module

[src/poob/music/spotify.py](../../src/poob/music/spotify.py) — `SpotifyPlaylistResolver`:

```python
class SpotifyPlaylistResolver:
    def __init__(self, client_id: str, client_secret: str): ...
    def is_configured(self) -> bool: ...
    async def resolve(self, url: str) -> list[dict] | None:
        """Returns list of {'title': str, 'artist': str} or None on failure."""
```

Internals:
- Parse playlist ID from URL (regex matches `open.spotify.com/playlist/<id>` and `spotify:playlist:<id>`; ignores query string)
- Call `spotipy.Spotify(auth_manager=SpotifyClientCredentials(...)).playlist_items(playlist_id, fields="items.track(name,artists(name)),next")`
- Page through `next` URL until exhausted (large playlists ≥100 tracks)
- For each item, extract `track.name` + first artist's name; skip `None` items (Spotify allows null entries in unavailable-track positions)
- Sync spotipy calls run in `asyncio.to_thread` since spotipy isn't async

All errors swallowed; the resolver returns `None` and logs at WARN with `error=str(exc)[:120]` — matches the existing cascade-tier pattern in [[music-autoplay-cascade]].

## Brain tool surface

One new action on `music_assistant`:

| Action | Args | Behavior |
|---|---|---|
| `queue_spotify_playlist` | `url: str` | Resolves the URL → searches each track on YT → enqueues. Reports `Queued N from Spotify (M not found).` |

Reuses the existing `url` arg if present, otherwise introduces a sibling under the same name. The tool description gets a line about the action; `url` arg description notes it accepts both web URLs and `spotify:playlist:<id>` URIs.

## Music cog dispatch

In [music_cog.py](../../src/poob/discord_bot/cogs/music_cog.py), one new `if action == "queue_spotify_playlist":` block. Resolver is lazy-constructed (one per cog) and cached. Per-track resolution mirrors the existing `queue_many` action's loop — search, enqueue if found, track not-found count, build a summary string.

Response shape:
- Not configured: `[SILENT]Spotify isn't configured. Set SPOTIPY_CLIENT_ID and SPOTIPY_CLIENT_SECRET to use playlist URLs.`
- Missing URL arg: `[SILENT]Spotify playlist URL?`
- Bad/non-Spotify URL: `[SILENT]That doesn't look like a Spotify playlist URL.`
- Empty playlist or all-unresolvable: `[SILENT]Couldn't find any tracks from that playlist.`
- Success: `[SILENT]Queued 23 from Spotify (2 not found).`

## TDD list

### `tests/unit/test_spotify_resolver.py`

1. `test_parse_playlist_id_from_web_url` — `https://open.spotify.com/playlist/abc123` → `abc123`.
2. `test_parse_playlist_id_from_uri` — `spotify:playlist:abc123` → `abc123`.
3. `test_parse_playlist_id_strips_query_string` — `...playlist/abc123?si=xyz` → `abc123`.
4. `test_parse_invalid_url_returns_none` — random URL → None.
5. `test_resolve_returns_title_artist_list` — mock spotipy's `playlist_items` to return 3 tracks; assert resolver returns 3 dicts with title+artist.
6. `test_resolve_pages_through_next` — first page has `next` set; second page is final. Assert both pages' items are returned.
7. `test_resolve_skips_null_track_items` — Spotify returns items with `track: null` (unavailable). Resolver skips them; reports the valid ones.
8. `test_resolve_handles_api_error_returns_none` — spotipy raises; resolver swallows and returns None.
9. `test_is_configured_requires_both_secrets` — only client_id set → False; both → True.

### `tests/unit/test_music_handler_actions.py` additions

10. `test_queue_spotify_playlist_not_configured_returns_soft_error` — resolver.is_configured()=False; soft message.
11. `test_queue_spotify_playlist_missing_url_returns_help`.
12. `test_queue_spotify_playlist_resolves_and_queues` — mock resolver to return 3 titles, mock ytdl to return 3 tracks; assert queue.add called 3 times; reply mentions count.
13. `test_queue_spotify_playlist_partial_resolution_reports_not_found` — 3 titles, ytdl returns track for 2 + None for 1; reply has "1 not found".

## Files to change

- [src/poob/config.py](../../src/poob/config.py) — add `spotify_client_id` + `spotify_client_secret` fields.
- [.env.example](../../.env.example) — add the two new keys with empty values + a comment.
- [pyproject.toml](../../pyproject.toml) — add `spotipy>=2.23.0`.
- [src/poob/music/spotify.py](../../src/poob/music/spotify.py) — NEW. `SpotifyPlaylistResolver`.
- [src/poob/discord_bot/cogs/music_cog.py](../../src/poob/discord_bot/cogs/music_cog.py) — wire resolver into `__init__`, add dispatch block.
- [src/poob/brain/poob.py](../../src/poob/brain/poob.py) — add `queue_spotify_playlist` to the action enum + `url` arg schema.
- [tests/unit/test_spotify_resolver.py](../../tests/unit/test_spotify_resolver.py) — NEW, 9 tests.
- [tests/unit/test_music_handler_actions.py](../../tests/unit/test_music_handler_actions.py) — extend with 4 dispatch tests.

## Acceptance

- ✅ All 13 new tests pass.
- ✅ Full unit suite stays green (1105+ baseline).
- ✅ Manual smoke (operator-side, after creds provisioned): paste a Spotify playlist URL in chat → "Poob queue this playlist" → tracks appear in queue.
- ✅ Decision note shipped; this plan archives to "Superseded / historical."

## Deferred follow-ups

- **Save Spotify-resolved tracks as a named playlist in one action.** Trivial composition (`queue_spotify_playlist` then `save_playlist`) — could add a `to_playlist: str` arg later if voice users want a one-shot.
- **Spotify track URL (single song)** — separate action `queue_spotify_track` with the same auth gating. Add when first user asks.
- **Spotify album URL** — same shape, different endpoint (`album_tracks`). Add when needed.
