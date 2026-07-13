---
type: incident
status: resolved
date: 2026-07-11
tags: [music, spotify, routing, brain]
related: [[music-spotify-playlist-import]] [[music-search-candidate-rerank]]
---

# Pasted Spotify track link → "play what?" — no path could handle it

## Symptom

Production `voice.log`, 2026-07-11 04:37:

```
04:37:54  poob.tool_route  args={'action': 'play', 'url': 'https://open.spotify.com/track/0gfk…'}
04:37:54  music.play empty query — prompting user query= user=…
04:38:00  (user, passive) "Yeah. It didn't it didn't work."
```

A user pasted a Spotify **track** link asking to play it. The router put the
link in `url` (the schema documented `url` for `queue_spotify_playlist`, so
that was its best instinct) — and the request died at the brain's
empty-play-query gate.

## Root cause (three gaps stacked)

1. **The play path only read `query`.** Both brain-level empty-query gates
   (`_handle_music` and the streaming variant) and the cog's play branch
   ignored a populated `url` arg entirely.
2. **The Spotify resolver was playlist-only.** `SpotifyPlaylistResolver` had
   no track-URL parsing or single-track metadata fetch. Tracks can't be
   streamed from Spotify (DRM), but their title+artist metadata resolves fine
   on the same free-tier client credentials.
3. **A Spotify *playlist* URL via `play` was a trap**: the play branch's
   generic `"/playlist" in query` check would route it to the YouTube
   playlist extractor, which cannot read Spotify.

## Fix

- **Brain gates** promote `url` → `query` (http/https/spotify: schemes only)
  before concluding a play request is empty — both text and voice-streaming
  paths.
- **`SpotifyPlaylistResolver.resolve_track(url)`** (+ `parse_track_id`):
  single-track URL → `{title, artist}` via `client.track()`, same
  error-contract as `resolve` (None on unparseable/unconfigured/API error).
- **Cog play branch**: falls back to `url_arg` when query is empty; a
  Spotify track link resolves to title+artist then flows through normal
  YouTube search; a Spotify playlist link delegates to the same flow as
  `queue_spotify_playlist` (loop extracted into the shared
  `_queue_spotify_playlist_url` helper); unconfigured/unreadable Spotify →
  honest "tell me the song name" instead of the non-sequitur "What do you
  want me to play?".
- **Schema** `url` description now tells the router pasted single-track
  links belong in `query` (`play` handles links) — belt to the handler's
  suspenders.

## Validation

- `tests/unit/test_spotify_resolver.py`: track-URL parsing (web + URI +
  query-string forms, no cross-match with playlist URLs), resolve_track
  happy/unconfigured/API-error/missing-name paths.
- `tests/unit/test_music_handler_actions.py`: url-arg fallback, track-link →
  metadata → search, honest errors, playlist-link delegation (and proof the
  YouTube playlist extractor is NOT called), original action unchanged via
  the shared helper.
- Post-deploy: pasting a Spotify track link with "play this" should queue
  the YouTube match; log signal `play: spotify track link resolved`.

## Review-caught refinements (2026-07-13)

The pre-commit adversarial review caught two gaps that would have re-opened
this same dead-end — both fixed and tested:

1. **Locale-prefixed share URLs didn't match.** Spotify's shared links have
   carried an `intl-<lang>` segment (`open.spotify.com/intl-de/track/…`,
   `intl-pt-br`) since ~2023 — a common form. Neither regex matched, so an
   intl link dead-ended in the YouTube extractor exactly as the original bug.
   **Fix:** the locale segment is now optional in both regexes —
   `open\.spotify\.com/(?:intl-[a-z-]+/)?(track|playlist)/`.
2. **The url→query promotion was scheme-only.** It accepted only
   `http/https/spotify:`, narrower than the resolver's own
   `AsyncYTDL._looks_like_url` (which also plays scheme-less `www.` /
   `youtube.com` / `youtu.be`), so a scheme-less YouTube link the router put
   in `url` dead-ended at "Play what?". **Fix:** a shared
   `_looks_like_playable_link` helper mirrors the resolver's recognition
   (plus `spotify:`), used at both brain play gates.

## Follow-ups

- Album links (`open.spotify.com/album/…`) still have no path — same
  resolver pattern applies if users ask for it.
