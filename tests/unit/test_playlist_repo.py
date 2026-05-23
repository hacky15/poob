"""Tests for GuildPlaylistsRepository.

Per-guild named-playlist store: save / load / list_names / delete with
case-insensitive name uniqueness and cross-guild isolation. See
[[music-named-playlists]] for the design.
"""

from __future__ import annotations

import json

import pytest

from poob.storage.repositories.playlist_repo import GuildPlaylistsRepository


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _t(title: str, vid: str | None = None) -> dict:
    """Build a minimal track dict matching the persisted shape."""
    return {
        "title": title,
        "url": f"https://youtu.be/{vid or title.replace(' ', '_')}",
        "identifier": vid or title.replace(" ", "_"),
        "duration_seconds": 180,
        "source": "youtube",
    }


# ---------------------------------------------------------------------------
# Roundtrip + basic CRUD
# ---------------------------------------------------------------------------

class TestPlaylistRepoCRUD:
    async def test_save_then_load_roundtrip(self, db_connection):
        repo = GuildPlaylistsRepository(db_connection)
        tracks = [_t("Bohemian Rhapsody"), _t("Don't Stop Believin'"), _t("Africa")]
        await repo.save("100", "chill", tracks)

        loaded = await repo.load("100", "chill")
        assert loaded == tracks

    async def test_load_missing_returns_none(self, db_connection):
        repo = GuildPlaylistsRepository(db_connection)
        result = await repo.load("100", "doesnt-exist")
        assert result is None

    async def test_list_names_empty_returns_empty_list(self, db_connection):
        repo = GuildPlaylistsRepository(db_connection)
        names = await repo.list_names("100")
        assert names == []

    async def test_list_names_sorted_alphabetical(self, db_connection):
        repo = GuildPlaylistsRepository(db_connection)
        # Save out of alphabetical order
        await repo.save("100", "sunday-morning", [_t("a")])
        await repo.save("100", "chill", [_t("b")])
        await repo.save("100", "gym", [_t("c")])

        names = await repo.list_names("100")
        assert names == ["chill", "gym", "sunday-morning"]


# ---------------------------------------------------------------------------
# Upsert semantics + case-insensitive naming
# ---------------------------------------------------------------------------

class TestPlaylistRepoUpsert:
    async def test_save_same_name_upserts(self, db_connection):
        repo = GuildPlaylistsRepository(db_connection)
        await repo.save("100", "chill", [_t("first")])
        await repo.save("100", "chill", [_t("second"), _t("third")])

        loaded = await repo.load("100", "chill")
        assert loaded == [_t("second"), _t("third")]

    async def test_save_same_name_case_insensitive(self, db_connection):
        repo = GuildPlaylistsRepository(db_connection)
        await repo.save("100", "Chill", [_t("first")])
        await repo.save("100", "chill", [_t("second")])

        # Loading with either case returns the latest content
        assert await repo.load("100", "Chill") == [_t("second")]
        assert await repo.load("100", "chill") == [_t("second")]
        assert await repo.load("100", "CHILL") == [_t("second")]

        # Only one row exists (no duplicates from different casings)
        names = await repo.list_names("100")
        assert len(names) == 1


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------

class TestPlaylistRepoDelete:
    async def test_delete_removes_playlist(self, db_connection):
        repo = GuildPlaylistsRepository(db_connection)
        await repo.save("100", "chill", [_t("a")])

        deleted = await repo.delete("100", "chill")
        assert deleted is True
        assert await repo.load("100", "chill") is None
        assert await repo.list_names("100") == []

    async def test_delete_nonexistent_returns_false(self, db_connection):
        repo = GuildPlaylistsRepository(db_connection)
        deleted = await repo.delete("100", "doesnt-exist")
        assert deleted is False


# ---------------------------------------------------------------------------
# Cross-guild isolation
# ---------------------------------------------------------------------------

class TestPlaylistRepoIsolation:
    async def test_cross_guild_isolation(self, db_connection):
        repo = GuildPlaylistsRepository(db_connection)
        guild_a_tracks = [_t("a"), _t("b"), _t("c")]
        guild_b_tracks = [_t("x"), _t("y")]

        await repo.save("100", "chill", guild_a_tracks)
        await repo.save("200", "chill", guild_b_tracks)

        # Each guild sees only its own content
        assert await repo.load("100", "chill") == guild_a_tracks
        assert await repo.load("200", "chill") == guild_b_tracks
        assert await repo.list_names("100") == ["chill"]
        assert await repo.list_names("200") == ["chill"]

        # Deleting in guild A doesn't touch guild B
        await repo.delete("100", "chill")
        assert await repo.load("100", "chill") is None
        assert await repo.load("200", "chill") == guild_b_tracks
