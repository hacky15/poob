---
type: decision
status: active
date: 2026-05-23
tags: [music, voice, brain, storage, playlists]
related: [[music-player-architecture]] [[music-named-playlists]] [[music-autoplay-cascade]] [[music-queue-primitives]] [[multi-guild-isolation]] [[music-bot-feature-roadmap]]
---

# Per-guild named playlists — single SQLite table keyed on `(guild_id, LOWER(name))`

## Context

[[music-bot-feature-roadmap]] section §9 — "Save / restore named playlists per server" — was the next item in the music ship order after [[music-autoplay-cascade]]. The feature is table stakes for an Aiode-style "personal jukebox" UX ("Poob, save this as `chill`" / "Poob, load `chill`") and unblocks the v2 of the Spotify-URL-import work (§8): a resolved Spotify playlist lands in the same store via the same brain-tool surface, so all "named track collections" flow through one canonical persistence layer.

The existing `UserPreferencesRepository` is per-user, not per-guild. The patrol-engine side has guild-aware stores (listings, deals) but no "this collection of music tracks belongs to guild X under name Y" surface. A new dedicated table keeps the playlist data out of the user-prefs key-value soup and gives us a clean place to extend (Spotify import results, synced-lyrics last-shown line, etc.) without bloating prefs.

## Decision

One new SQLite table, one new repository, four new brain-tool actions, optional cog wiring.

**Schema** (added to `database.py`'s bootstrap):

```sql
CREATE TABLE IF NOT EXISTS guild_playlists (
    id           TEXT PRIMARY KEY,
    guild_id     TEXT NOT NULL,
    name         TEXT NOT NULL,
    tracks_json  TEXT NOT NULL,       -- JSON list of track dicts
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE UNIQUE INDEX idx_guild_playlists_unique
    ON guild_playlists(guild_id, LOWER(name));
```

- **Name uniqueness is case-insensitive** but the user's casing is preserved for display. `Chill` and `chill` collapse to the same row; first writer wins on display casing. Reasoning: voice-side STT capitalization is unreliable, but the user wants their name to look like they typed it.
- **`tracks_json`** stores a list of `{title, url, identifier, duration_seconds, source, is_stream}` dicts. Stream URLs are intentionally NOT stored — they expire ~6 hours on YouTube; the player re-resolves at play time via `AsyncYTDL` per [[music-player-architecture]]. Saving stale URLs would just guarantee future playback failures.
- **`guild_id` partition** in every query gives cross-guild isolation by construction. Matches the pattern in [[multi-guild-isolation]] and is verified by an explicit isolation test.

**Repository** (`GuildPlaylistsRepository` in `src/poob/storage/repositories/playlist_repo.py`):

```python
async def save(guild_id: str, name: str, tracks: list[dict]) -> None     # upsert
async def load(guild_id: str, name: str) -> list[dict] | None            # None if missing
async def list_names(guild_id: str) -> list[str]                         # alphabetical
async def delete(guild_id: str, name: str) -> bool                       # True if removed
```

Upsert leverages SQLite's `ON CONFLICT(guild_id, LOWER(name)) DO UPDATE`. Single round-trip, no read-modify-write race window. The unique index makes the conflict-target valid without a separate compound primary key.

**Brain tool surface**: four new actions on the existing `music_assistant` tool, plus one new arg.

| Action | Args | Behavior |
|---|---|---|
| `save_playlist` | `name: str` | Persists current track + upcoming queue under `name`. Reports `Saved` or `Updated` based on whether the name existed. Rejects empty queues with `[SILENT]Nothing to save — queue is empty.` |
| `load_playlist` | `name: str` | Appends saved tracks to the queue (does NOT interrupt current playback). Reports `Loaded playlist 'X' — N tracks queued.` |
| `list_playlists` | _none_ | Returns alphabetical name list. Friendly `No saved playlists yet.` when empty. |
| `delete_playlist` | `name: str` | Removes the playlist. Soft errors with `No playlist named 'X'.` when missing. |

All four return `[SILENT]…` reply strings so the brain composes a natural narration around them, matching the dispatch shape used by `apply_effect` and `autoplay`.

**Cog wiring**: `MusicCog.__init__` takes a new optional `playlist_repo` parameter. When `None` (current default; production wires one in via `main.py`), the four playlist actions soft-error with `[SILENT]Playlists aren't configured on this stack yet.` — keeps existing test fixtures working without a DB, and gives a clear failure mode if the wiring slips in a future refactor.

## Alternatives considered

- **Reuse `user_preferences` as a key-value store under a `playlist:<name>` key.** Rejected: per-user (we want per-guild); collides with the per-user-prefs idea of "settings for me." Hard to query "all playlists for guild X." Hard to add per-playlist metadata (created_at, source) without further key-conventional gymnastics.
- **A `guild_preferences` general-purpose key-value table** (mirroring `user_preferences`). Rejected for now: nothing else needs per-guild persisted KV today. If/when autoplay-state or per-guild defaults need persistence, we'll add it then; making one table do double duty up-front is YAGNI.
- **Store tracks as separate rows** (`playlist_tracks(playlist_id, position, title, …)`). Cleaner relational shape, but the JSON-blob approach is simpler, atomic on read/write, and matches how `Track` is reconstructed in code (single dict-to-dataclass mapping). The relational shape adds value only if we need per-track query (rename-track-in-playlist, etc.) — explicitly out of scope per the plan note.
- **Compose Spotify import directly into the brain tool surface without persistence** (item §8 of the roadmap as a pure pass-through). Rejected: persistence is what makes Spotify import useful — the user wants the playlist to *survive* the resolution step, not vanish after one playthrough. Building this layer first lets §8 land cleanly on top.

## Consequences

- ✅ 4 new brain actions + 1 new arg, on top of the existing `music_assistant` tool. Token cost in tool-schema prompts is ~50 tokens — negligible vs the cascade's ~120-token MUSIC_TOOL baseline.
- ✅ One new SQLite table, no migration story needed — `CREATE TABLE IF NOT EXISTS` is idempotent, ships on first bot start after the deploy.
- ✅ The roadmap's §8 (Spotify URL import) and §10 (synced lyrics) both compose on top: §8 writes resolved Spotify-playlist tracks via `save()`; §10 references the live `current_track` independent of any saved playlist. No cross-feature coupling.
- ⚠️ JSON-blob storage means playlist content isn't queryable with SQL filters ("find all playlists containing X"). Acceptable for v1; document if usage ever needs cross-playlist search.
- ⚠️ Voice-side users have to say playlist names out loud. STT may garble unusual names ("sunday-morning" → "Sunday morning") — case-insensitive lookup handles capitalization but doesn't handle word boundary differences. Defer fuzzy-match until a real user complains.
- ⚠️ No backup story. A `DROP TABLE` or `rm scraper.db` wipes every playlist. Operationally fine for single-server homelab use; revisit if multi-server / public-bot deployment becomes a thing.

## Rollback

Single-commit revert restores the previous state. The new table sticks around in the DB (no migration to undo) but nothing reads it after revert; orphan rows are cosmetic. Removing `guild_playlists` would need a manual `DROP TABLE` — not worth automating for a v1 with one user.

If the feature ships but someone wants it off without a code revert: drop the brain-tool actions from `MUSIC_TOOL`'s enum and the LLM will stop emitting them. The dispatch blocks become dead code until re-enabled. Zero-downtime, single-commit gate.

## References

- Originating plan: [[music-named-playlists]]
- Storage pattern parallel: `UserPreferencesRepository` (the templating reference)
- Multi-guild isolation: [[multi-guild-isolation]]
- Brain-tool dispatch pattern reused: [[music-autoplay-cascade]] (cascade engine + tool action) and [[music-queue-primitives]] (queue mutation actions)
- Music feature roadmap: [[music-bot-feature-roadmap]] §9 (this item) and §8/§10 (the upcoming features that compose on top)
