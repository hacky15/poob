---
type: decision
status: active
date: 2026-05-23
tags: [music, voice, brain, spotify, ytdl]
related: [[music-player-architecture]] [[music-spotify-playlist-import]] [[music-named-playlists]] [[music-autoplay-cascade]] [[music-bot-feature-roadmap]] [[music-queue-many-tool]]
---

# Spotify playlist import — `ClientCredentials` flow + YT resolution (no OAuth, no Premium)

## Context

[[music-bot-feature-roadmap]] section §8 — "Spotify playlist URL import" — is the next item in the music ship order after [[music-named-playlists]] landed. The user-facing UX: voice or text user pastes a Spotify playlist URL and the bot queues each track. Multi-source playlist import is what separates "AI-augmented" music bots (Aiode, Hydra) from the "simple/fast" tier.

Spotify deprecated several recommendation endpoints in late 2024 and tightened developer access in Feb 2026 (per the roadmap citation), but the deprecations targeted `/recommendations` and `/audio-features` — NOT the `playlist_items` endpoint. Read-only public-playlist metadata reads still operate on the free `ClientCredentials` flow with no Premium and no per-user OAuth. That's the lane we use.

## Decision

A single new brain action `queue_spotify_playlist(url)` that runs a two-stage resolution:

1. **Spotify side** — `SpotifyPlaylistResolver.resolve(url)` parses the URL (web form or URI), authenticates via `spotipy.SpotifyClientCredentials`, calls `playlist_items` with field selection `items.track(name,artists(name)),next`, pages through every `next` URL until exhausted, returns `list[{title, artist}]`. Skips null-track items (Spotify includes them for catalog-pulled tracks). Synchronous spotipy calls run under `asyncio.to_thread` so the bot's event loop stays responsive.

2. **YouTube side** — for each `{title, artist}`, build a query `"<title> <artist>"` and run it through the existing `AsyncYTDL.search`. Successful resolutions get enqueued via `player.play(track, deferred=voice)`; unresolved ones accumulate into a not-found list. Final reply summarizes resolved + not-found counts.

Configuration is two new optional `AppConfig` fields (`spotify_client_id`, `spotify_client_secret`) bound to `SPOTIFY_CLIENT_ID` / `SPOTIFY_CLIENT_SECRET` env vars. When either is empty, `SpotifyPlaylistResolver.is_configured()` returns False and the brain action soft-errors with a friendly message — no crash, no silent ignore.

The resolver lives in [src/poob/music/spotify.py](../../src/poob/music/spotify.py). Spotipy is a lazy import (only fires when the resolver actually calls Spotify) so tests pass without the package installed and the bot boots cleanly even if `spotipy` ends up missing from the image.

## Alternatives considered

- **Send the user to Spotify OAuth and persist a refresh token** to unlock the user's saved-tracks library. Rejected: huge lift (per-user OAuth flow, token rotation, expiry handling, encrypted storage) for a feature whose value (public playlists) we already cover with the free path. Add OAuth only if a user asks for "my saved tracks" specifically.
- **Use yt-dlp's `--match-filter` against `youtube_music_search1:<title>`** instead of `AsyncYTDL.search`. Equivalent latency and quality; we already have the search helper wired with the two-pass ytsearch1→ytsearch5 widening from [[ytdl-search-best-guess-fallback]], which is better than rolling new yt-dlp invocation patterns inside this module.
- **Crawl `open.spotify.com` HTML for track titles instead of using the API.** Brittle (HTML structure shifts), ToS-questionable, and slower than the API. The API is free for what we need.
- **Resolve to non-YouTube sources** (SoundCloud, Bandcamp, Apple Music). Out of scope; would require sibling resolver modules + a source-selection cascade. Add if the YouTube resolution rate gets bad.
- **Auto-save resolved Spotify playlists to the `guild_playlists` store** via [[music-named-playlists]]. Possible but premature: voice users haven't asked for "save what I just imported." If they do, a `to_playlist: str` arg on `queue_spotify_playlist` would handle it without a new action.

## Consequences

- ✅ One new brain action, one new arg (`url`), one new optional config block. Zero existing surfaces broken; gating is graceful when creds are unset.
- ✅ Composes cleanly with [[music-named-playlists]] (operator can manually `save_playlist` after a Spotify import to persist the resolved tracks) and [[music-queue-many-tool]] (the queue-and-summarize loop is the same shape).
- ✅ Free tier — no per-user OAuth, no Premium, no quota walls on `playlist_items` reads for public playlists at our usage levels.
- ⚠️ Spotipy is an unofficial-API-leaning library; major Spotify Web API changes could break it. Mitigation: lazy import + try/except around every Spotify call + soft-error fallback. Failures don't crash the bot; they degrade to "couldn't find any tracks."
- ⚠️ YouTube resolution rate isn't 100% — songs with weird Unicode in titles, region-locked content, or generic artist names sometimes resolve to the wrong YT video. The two-pass `ytsearch1→ytsearch5` fallback ([[ytdl-search-best-guess-fallback]]) mitigates but doesn't eliminate. Track-level mistakes are user-visible in the queue.
- ⚠️ Large playlists (500+ tracks) take 1-3 minutes to resolve sequentially — one `ytdl.search` per track. Acceptable for v1; if it becomes a complaint, parallelize with `asyncio.gather` (bounded concurrency to avoid YT rate-limiting).

## Rollback

Single-commit revert restores the previous state. The new env vars become inert if `SpotifyPlaylistResolver` is never constructed. Removing `queue_spotify_playlist` from the brain-tool enum is a one-line change that gates the action without touching code paths the resolver still exists for.

If `spotipy` itself starts breaking against current Spotify API: the lazy import means the bot still boots; only this action soft-errors. Operator can disable via env-var clear (set both to empty) without redeploy.

## References

- [[music-spotify-playlist-import]] — originating plan
- [[music-bot-feature-roadmap]] §8 (the ship-order entry) and the platform-restrictions citation establishing that `playlist_items` survived the 2026-02 changes
- [src/poob/music/spotify.py](../../src/poob/music/spotify.py) — the resolver
- [src/poob/discord_bot/cogs/music_cog.py](../../src/poob/discord_bot/cogs/music_cog.py) — dispatch
- [spotipy docs](https://spotipy.readthedocs.io/) — `ClientCredentials` flow + `playlist_items` reference
