---
type: decision
status: active
date: 2026-05-01
tags: [music, ytdl, search, voice]
related: [[music-player-architecture]] [[one-handler-music-contract]] [[voice-three-failure-modes-april27]]
---

# Music search: two-pass fallback (ytsearch1 → ytsearch5) instead of "couldn't find"

## Context

Production logs at 02:02 on 2026-05-01 showed a user request misheard by STT as `Ambatakam to Marwani` (probably "Ambatukam" or similar). yt-dlp's default `ytsearch1:` couldn't surface a tight enough match, returned empty entries, and the music handler returned `"Couldn't find anything for 'Ambatakam to Marwani'."` Toob then mocked the user about wanting Indian electronica with no actual song to play.

User feedback: *"why doesn't it just give the best guess for what it interprets the user saying?"* — correct framing. Even a best-guess wrong song is a better experience than dead silence with a snide one-liner.

## Decision

`YTDLProvider.search` now does a two-pass best-guess for non-URL queries:

1. **Pass 1**: `extract_info(query)` — `default_search: auto` wraps it as `ytsearch1:`.
2. **Pass 2 (fallback)**: if pass 1 returns `None` or empty entries AND the query is not a URL, retry `extract_info("ytsearch5:" + query)` and take the first viable entry.
3. Only returns `None` if both passes fail.

Direct URLs skip the fallback — if `https://youtube.com/...` doesn't extract, the URL is the source of truth and a relaxed search would surface a different song entirely.

A new helper `_first_track_from_info` consolidates the entries-list-vs-single-result handling so both passes use the same extraction path.

## Why ytsearch5 as the widening step

YouTube's search engine routinely returns near-matches when given fuzzy text. `ytsearch1` is conservative and may discard candidates yt-dlp considers low-confidence; `ytsearch5` returns the top 5 results and yt-dlp emits all of them. Picking the first non-None is exactly the "best guess" the user asked for.

## Logging signals

- `ytdl.search fallback to ytsearch5 query=...` — first pass came back empty, fallback firing.
- `ytdl.search recovered via ytsearch5 query=... picked=...` — fallback succeeded, what got picked.

If the picked track is consistently wrong, the logs make this visible without touching the bot's user-facing behavior. Future tuning could add ytsearch10 or a phonetic-relaxed retry, but only after the data shows it's needed.

## Why not strip "modifier" words from the query

Considered: when ytsearch1 fails for `Ambatakam to Marwani`, drop "to Marwani" and try `Ambatakam`. Rejected because:

- It bakes English-grammar assumptions into a global music search ("to" is a stop word in English; in transliterated Tamil/Indian song titles it's frequently part of the actual title).
- ytsearch5 is doing the same job at the YouTube-search layer, where it's language-agnostic.
- Adds no new failure modes; the two-pass approach is a strict superset of the old behavior.

## Why not allow "Couldn't find" anymore

The "Couldn't find" path still exists and returns when both passes find nothing. That happens for genuinely degenerate queries (single character, blocked region, network failure, yt-dlp throwing). The user's complaint was about *common* misheard queries — the fallback addresses those. We don't need to lie that we played something when YouTube genuinely returned zero candidates.

## Validation

- 23 unit tests in the music + ytdl scope pass.
- Watch for in production:
  - `ytdl.search fallback to ytsearch5` events on misheard queries.
  - Followed by `ytdl.search recovered via ytsearch5` rather than `Couldn't find anything` on the next line.
  - Subjective: Toob's wrap is followed by audio playing, even if the song is a near-match rather than the exact request.

## Rollback

Single-line revert in `YTDLProvider.search` — remove the fallback block. The `_first_track_from_info` helper and `_looks_like_url` helper can stay; they're inert without the fallback caller.

## Architectural integrity

- **One-handler music contract** ([[one-handler-music-contract]]) — unchanged. `MusicCog.handle_music_request` still calls `YTDLProvider.search` exactly once.
- **Speculative wrap** ([[speculative-music-wrap]]) — unaffected. The wrap streams while ytdl runs in the background; this change just makes the background task succeed more often.
- **Music handler return contract** — unchanged. Returns the same shape ("Playing X..." / "Couldn't find anything for X").
