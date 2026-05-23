---
type: plan
status: active
date: 2026-05-23
tags: [music, voice, brain, storage, playlists]
related: [[music-player-architecture]] [[music-bot-feature-roadmap]] [[music-queue-primitives]] [[music-autoplay-cascade]] [[multi-guild-isolation]]
---

# Named per-guild playlists — save / load / list / delete

## Goal

Aiode-style "personal jukebox" UX: a user says *"Poob, save this as `chill`"* and later *"Poob, load `chill`"*. Persists across bot restarts. Per-guild scoped — same name in two different servers is two different playlists (matches the existing per-guild storage pattern in [[multi-guild-isolation]]).

This is item #9 from [[music-bot-feature-roadmap]]'s ship order. Foundational for #8 (Spotify import lands here too — a Spotify URL resolves into tracks and gets saved as a named playlist) and naturally extends the brain-tool dispatch pattern used by [[music-autoplay-cascade]].

## Storage

New SQLite table in [src/poob/storage/database.py](../../src/poob/storage/database.py):

```sql
CREATE TABLE IF NOT EXISTS guild_playlists (
    id TEXT PRIMARY KEY,
    guild_id TEXT NOT NULL,
    name TEXT NOT NULL,
    tracks_json TEXT NOT NULL,      -- JSON list of {title, url, identifier, duration_seconds, source}
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_guild_playlists_unique
    ON guild_playlists(guild_id, LOWER(name));
```

Case-insensitive name uniqueness per guild (so `chill` and `Chill` are the same playlist). `tracks_json` is the canonical representation — list of `Track`-shaped dicts (only the fields we need to rebuild a playable track later: `title`, `url`, `identifier`, `duration_seconds`, `source`). Stream URLs are NOT stored (they expire ~6 hours on YouTube; `MusicQueue.get_next()` re-resolves them via `AsyncYTDL` at play time, same as today).

New repo: [src/poob/storage/repositories/playlist_repo.py](../../src/poob/storage/repositories/playlist_repo.py) — `GuildPlaylistsRepository` with:

- `save(guild_id: str, name: str, tracks: list[dict]) -> None` — upsert
- `load(guild_id: str, name: str) -> list[dict] | None` — None if missing
- `list_names(guild_id: str) -> list[str]` — sorted alphabetically
- `delete(guild_id: str, name: str) -> bool` — True if deleted, False if missing

Connection injection follows the existing pattern (see [[multi-guild-isolation]] or `UserPreferencesRepository` as templates).

## Brain tool surface

Four new actions added to `MUSIC_TOOL` in [src/poob/brain/poob.py](../../src/poob/brain/poob.py):

- `save_playlist` — saves current queue (current track + upcoming) under `name`. Args: `name: str`.
- `load_playlist` — appends a saved playlist's tracks to the current queue. Args: `name: str`.
- `list_playlists` — returns alphabetical list of playlist names for the guild. No args.
- `delete_playlist` — removes a saved playlist. Args: `name: str`.

Adds a new top-level arg to `MUSIC_TOOL`: `name: str` (only consumed by these four actions). Same dispatch pattern as `apply_effect`'s `effect` and `autoplay`'s `mode`.

## Music cog dispatch

In [src/poob/discord_bot/cogs/music_cog.py](../../src/poob/discord_bot/cogs/music_cog.py), four new `if action == "X":` blocks. Need a `GuildPlaylistsRepository` reference — wire it through `MusicCog.__init__` from the bot's existing DB connection.

Response shape mirrors the autoplay/effect dispatchers — `[SILENT]Saved playlist 'chill' with 12 tracks.` / `[SILENT]Loaded playlist 'chill' — 12 tracks queued.` / `[SILENT]Playlists: chill, deep-focus, gym, sunday-morning.` / `[SILENT]Deleted playlist 'chill'.` Plus the standard missing-arg help: `[SILENT]Which playlist? Say 'list playlists' to see what you have.`

## Edge cases

- **Save with empty queue.** Reject: `[SILENT]Nothing to save — queue is empty.` Don't silently save an empty playlist.
- **Save with current track but empty upcoming.** Save the single track. Useful when the user wants to "remember this one."
- **Load nonexistent name.** Soft error: `[SILENT]No playlist named 'chill'.`
- **Load with currently-playing music.** Append to queue, don't interrupt current playback. The next track plays normally per [[music-queue-primitives]].
- **Save with name that already exists.** Upsert (overwrite). Reply: `[SILENT]Updated playlist 'chill' with 14 tracks.` (note "Updated" vs "Saved"). The semantics are user-visible so they know they overwrote.
- **Name normalization.** Strip whitespace, lowercase for uniqueness checks but preserve original casing for display. So `"  Chill  "` and `"chill"` collide; first writer wins on display casing.
- **Cross-guild isolation.** `guild_id` partition key in every query. A test must explicitly verify guild-A's playlists are invisible to guild-B.

## TDD list

In TDD order — each row is one failing test → minimal impl that makes it green.

### `tests/unit/test_playlist_repo.py`

1. `test_save_then_load_roundtrip` — save a list of 3 tracks under name "chill" in guild "100"; load returns the same 3 tracks.
2. `test_load_missing_returns_none` — `load("100", "doesnt-exist")` returns None.
3. `test_list_names_empty_returns_empty_list` — guild with no playlists returns `[]`.
4. `test_list_names_sorted_alphabetical` — save 3 playlists with names out of order; `list_names` returns alphabetically sorted.
5. `test_save_same_name_upserts` — save name "chill" twice; second save replaces the first; load returns the second.
6. `test_save_same_name_case_insensitive` — save "Chill" then "chill"; load with either case returns the latest content.
7. `test_delete_removes_playlist` — save then delete; load returns None; `delete` returns True.
8. `test_delete_nonexistent_returns_false` — delete a name that doesn't exist; returns False; no exception.
9. `test_cross_guild_isolation` — save "chill" in guild "100" with 3 tracks, save "chill" in guild "200" with 2 tracks; loading from each guild returns only that guild's content.

### `tests/unit/test_music_handler_actions.py` additions

10. `test_save_playlist_with_queue_persists_and_replies` — mock the repo, action=save_playlist name=chill; verify repo.save called with current track + upcoming; `[SILENT]Saved playlist 'chill' with N tracks.`
11. `test_save_playlist_with_empty_queue_rejects` — empty queue → `[SILENT]Nothing to save — queue is empty.`; repo.save NOT called.
12. `test_save_playlist_existing_name_says_updated` — mock repo to indicate name existed; reply says "Updated" not "Saved".
13. `test_save_playlist_missing_name_returns_help` — action=save_playlist with no name → `[SILENT]Name your playlist?`
14. `test_load_playlist_appends_to_queue` — mock repo.load to return 3 tracks; verify queue.add called 3 times; `[SILENT]Loaded playlist 'chill' — 3 tracks queued.`
15. `test_load_playlist_missing_returns_silent_error` — repo.load returns None; `[SILENT]No playlist named 'chill'.`
16. `test_load_playlist_missing_name_returns_help` — action=load_playlist with no name.
17. `test_list_playlists_returns_alphabetical_names` — repo.list_names returns 3 names; reply is comma-joined.
18. `test_list_playlists_empty_returns_friendly_message` — repo.list_names returns []; `[SILENT]No saved playlists yet.`
19. `test_delete_playlist_removes_and_replies` — repo.delete returns True; `[SILENT]Deleted playlist 'chill'.`
20. `test_delete_playlist_missing_returns_silent_error` — repo.delete returns False; `[SILENT]No playlist named 'chill'.`
21. `test_delete_playlist_missing_name_returns_help` — action=delete_playlist with no name.

## Files to change

- [src/poob/storage/database.py](../../src/poob/storage/database.py) — add `CREATE TABLE guild_playlists` to the schema bootstrap.
- [src/poob/storage/repositories/playlist_repo.py](../../src/poob/storage/repositories/playlist_repo.py) — NEW. `GuildPlaylistsRepository` class.
- [src/poob/storage/repositories/__init__.py](../../src/poob/storage/repositories/__init__.py) — export the new repo.
- [src/poob/discord_bot/cogs/music_cog.py](../../src/poob/discord_bot/cogs/music_cog.py) — wire repo into `__init__`, add 4 dispatch blocks.
- [src/poob/brain/poob.py](../../src/poob/brain/poob.py) — add 4 actions to the enum + `name` arg schema + tool description.
- [tests/unit/test_playlist_repo.py](../../tests/unit/test_playlist_repo.py) — NEW, 9 tests.
- [tests/unit/test_music_handler_actions.py](../../tests/unit/test_music_handler_actions.py) — extend with 12 dispatch tests.

## Acceptance

- ✅ All 21 new tests pass; existing music + storage suite stays green (no regressions to the 1080+ baseline).
- ✅ Manual smoke: in voice channel, play 3 songs → "Poob save as testlist" → "Poob list playlists" → "Poob load testlist" → expect the 3 tracks appended.
- ✅ Decision note shipped with the implementation; this plan archives to "Superseded / historical" in the plans MOC.
- ✅ Per-guild isolation visible in test 9 — no cross-guild leakage.

## Out of scope (deferred)

- **Public playlists shared across guilds** — would need a separate sharing model + invitation flow. Not requested.
- **Per-track metadata edit** (rename, reorder within saved playlist) — defer until usage shows demand. For now the playlist is immutable after save; users delete + re-save to change.
- **Playlist size cap** — no limit in v1. If someone saves a 10,000-track playlist, JSON blob gets big but SQLite handles it. Revisit if anyone hits operational issues.
- **Playlist sharing buttons in now-playing embed** — fits the [[music-now-playing-embed-buttons]] view but adds a button to an already-full layout. Defer.
- **Spotify URL import** (#8) and **Synced lyrics overlay** (#10) — separate plan notes; will land in follow-up commits using this playlist store as the canonical place for resolved tracks.
