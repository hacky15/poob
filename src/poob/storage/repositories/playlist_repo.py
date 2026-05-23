"""Repository for per-guild named playlists.

Each playlist is a JSON-encoded list of track dicts stored under a
``(guild_id, name)`` key with case-insensitive name uniqueness. Stream
URLs are intentionally NOT persisted — they expire ~6 hours on YouTube
and the player re-resolves them at play time anyway. See
``docs/plans/music-named-playlists.md`` for the surrounding design.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import aiosqlite


class GuildPlaylistsRepository:
    """Per-guild named-playlist store backed by SQLite.

    Names are stored as the user typed them (preserved casing) but
    uniqueness + lookups are case-insensitive via the
    ``idx_guild_playlists_unique(guild_id, LOWER(name))`` unique index.
    """

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def save(
        self, guild_id: str, name: str, tracks: list[dict],
    ) -> None:
        """Upsert a playlist. Overwrites existing content for the same
        ``(guild_id, LOWER(name))`` pair.
        """
        now = datetime.now(timezone.utc).isoformat()
        tracks_json = json.dumps(tracks)
        await self._conn.execute(
            """
            INSERT INTO guild_playlists (id, guild_id, name, tracks_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(guild_id, LOWER(name))
            DO UPDATE SET
                tracks_json = excluded.tracks_json,
                name = excluded.name,
                updated_at = excluded.updated_at
            """,
            (str(uuid.uuid4()), guild_id, name, tracks_json, now, now),
        )
        await self._conn.commit()

    async def load(self, guild_id: str, name: str) -> list[dict] | None:
        """Return the playlist's tracks, or ``None`` if missing."""
        cursor = await self._conn.execute(
            "SELECT tracks_json FROM guild_playlists "
            "WHERE guild_id = ? AND LOWER(name) = LOWER(?)",
            (guild_id, name),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return json.loads(row["tracks_json"])

    async def list_names(self, guild_id: str) -> list[str]:
        """Return the guild's saved-playlist names, sorted alphabetically."""
        cursor = await self._conn.execute(
            "SELECT name FROM guild_playlists "
            "WHERE guild_id = ? ORDER BY LOWER(name) ASC",
            (guild_id,),
        )
        rows = await cursor.fetchall()
        return [row["name"] for row in rows]

    async def delete(self, guild_id: str, name: str) -> bool:
        """Delete a playlist. Returns True if a row was removed."""
        cursor = await self._conn.execute(
            "DELETE FROM guild_playlists "
            "WHERE guild_id = ? AND LOWER(name) = LOWER(?)",
            (guild_id, name),
        )
        await self._conn.commit()
        return cursor.rowcount > 0
