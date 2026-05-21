---
type: decision
status: active
date: 2026-05-20
tags: [music, voice, autoplay, ytmusicapi, ytdl, cascade]
related: [[music-player-architecture]] [[music-autoplay]] [[music-bot-feature-roadmap]] [[search-provider-cascade]] [[one-handler-music-contract]]
---

# Autoplay cascade — `ytmusicapi.get_watch_playlist` → yt-dlp mix URL → history shuffle

## Context

Top free Discord music bots (FredBoat, Hydra, Aiode) ship "autoplay" as a queue-empty fallback that keeps the music going. Vexera and Mee6 gate it behind premium — shipping it free is differentiating. The user explicitly asked for it ("we need autoplay"). The 2026-02 Spotify dev-platform restrictions closed the door on Spotify's recommendation endpoints for hobby bots ([[music-bot-feature-roadmap]]), so the question collapses to "which YouTube-side recommender, and how do we cascade so a single failure doesn't dead-end playback?"

Poob already has a multi-tier cascade pattern in [[search-provider-cascade]] (Tavily → Serper → SearXNG → empty) and the [[vlm-triage-pipeline]] (Gemini Flash → Gemma → Groq → OpenRouter → Ollama). The autoplay engine follows the same shape: tier-ordered, fail-forward, each tier's exception caught + logged, last tier guaranteed to return something usable.

## Decision

Three tiers, in this order, behind a single ``AutoplayEngine.get_next(seed)`` coroutine that the player loop awaits when the queue empties and ``autoplay_enabled`` is set:

1. **`ytmusicapi.YTMusic().get_watch_playlist(videoId=seed.identifier, limit=10)`** — YouTube Music's own recommender via the `RDAMVM<videoId>` mix prefix. Picks the first candidate that isn't the seed and isn't already in recent history. Resolves the videoId to a full `Track` via `AsyncYTDL.search("https://youtu.be/<id>")`.
2. **yt-dlp on `https://www.youtube.com/watch?v=<id>&list=RD<id>`** — the plain-YouTube mix URL. `AsyncYTDL.extract_playlist(url)` returns the playlist entries; iterate and pick the first non-seed, non-historical entry. Slower (1-3 s) but doesn't depend on `ytmusicapi`'s unofficial-API surface.
3. **History shuffle** — random pick from the caller-supplied history list (`MusicQueue.history`) that isn't the seed. Guarantees playback never dead-ends due to network/API outage.

The engine wraps every tier in `try/except` and logs failures at WARN with the message trimmed to 120 chars. It returns `Track | None` and never raises. On `None`, the existing queue-empty break path stands.

Per-guild runtime state (`GuildMusicPlayer.autoplay_enabled: bool`, default `False`). No SQLite persistence — matches `loop_mode` / `shuffle` / `volume` which also reset on bot restart. The brain tool surface exposes `mode: "on" | "off" | "status"` on `music_assistant(action="autoplay", ...)`; the cog dispatcher mutates `player.autoplay_enabled` and returns a `[SILENT]` confirmation.

## Alternatives considered

- **Spotify `/recommendations`** — closed off for hobby bots since 2026-02 (Premium-only dev mode, 5-user cap, extended quota requires 250k MAU + registered business). Reject. ([[music-bot-feature-roadmap]] cites the platform-access blog post.)
- **Last.fm `track.getSimilar` as a tier** — works fine without auth but returns track+artist pairs that we'd then have to resolve back to YouTube via title search. Adds a network hop and a string-match resolution step where the wrong song can land. Skip until ytmusicapi fragility forces our hand; this is queued for a v2 of autoplay in [[music-autoplay]].
- **LLM-as-DJ "vibe" mode** — feed last 5 tracks to a chat model, ask for the next title. Slow (~1-2 s) and pays an LLM call per autoplay tick. Cool but premature. Queued in [[music-autoplay]] as a "vibe" sub-mode behind a third enum value.
- **Two-model ensemble or Lavalink-style queue** — Lavalink ships an `autoplay` flag native, but [[music-player-architecture]] already rejects Lavalink (no PCM mixer access, JVM overhead). Building the cascade in our existing AsyncYTDL stack is cheaper.
- **Persisting autoplay to SQLite for cross-restart memory** — would need a `guild_preferences` table that doesn't exist yet (the current `preferences_repo` is per-user, not per-guild). Defer until the toggle's stickiness shows real value. Pattern matches volume / loop / shuffle, all of which are runtime-only today.

## Consequences

- ✅ Free, fast (sub-second when ytmusicapi is healthy), and visible to users via voice ("Poob, turn on autoplay") or text mention.
- ✅ Survives single-tier failures — ytmusicapi unofficial API can break on YT-side schema shifts; the yt-dlp mix URL is a known-stable backup, and history shuffle is the last-resort guarantee.
- ✅ One new dependency (`ytmusicapi>=1.10.0`, MIT-licensed, no auth). Lazy-imported so the bot boots even if it's missing.
- ⚠️ Each autoplay-injected track adds ~1-3 s of resolution latency on the queue-empty boundary. That's between tracks anyway, so unnoticed. Worth watching if user reports "long silences when queue runs out."
- ⚠️ Recent-history filtering uses a small `set` of identifiers — won't deduplicate against tracks resolved by title-only (no identifier). Edge case unlikely to matter in practice; if it does, fallback is to enrich the history filter with a fuzzy-title check.
- ⚠️ Per-guild state resets on bot restart. Users who keep autoplay on permanently will need to re-toggle after every deploy. Documented as a known limitation in [[music-autoplay]] under "deferred."

## Rollback

Two ways:

1. **Disable via env, no code change** — the brain tool surface emits `action="autoplay"` with `mode="off"` to flip the flag back. The dispatcher honors it without restart.
2. **Disable globally if the cascade misbehaves** — set the player's `autoplay_enabled = False` as the default (already the default) and either remove the `"autoplay"` enum entry from `MUSIC_TOOL` or have the dispatcher short-circuit. Single-commit revert.

If `ytmusicapi` itself becomes unmaintainable (YT-side schema shift breaks it), the engine falls through to yt-dlp's mix URL tier automatically; no operator action needed.

## References

- [[music-bot-feature-roadmap]] §6 — the autoplay-cascade ranking that informed this ordering
- [[search-provider-cascade]] — the established pattern for tier-ordered fail-forward services
- [[music-player-architecture]] — the queue-empty hook now extends to call this engine
- [src/poob/music/autoplay.py](../../src/poob/music/autoplay.py) — the engine
- [docs/plans/music-autoplay.md](../plans/music-autoplay.md) — the originating implementation plan
- [ytmusicapi docs](https://ytmusicapi.readthedocs.io/) — upstream API surface for tier 1
