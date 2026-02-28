"""Repository for user preferences CRUD operations."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import aiosqlite


class UserPreferencesRepository:
    """Key-value preference storage per Discord user.

    Preferences are stored as JSON strings under well-known keys
    (e.g. 'wishlist', 'location', 'search_priorities').

    Args:
        conn: aiosqlite database connection.
    """

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def get(self, discord_user_id: str, key: str) -> str | None:
        """Get a single preference value.

        Returns:
            The JSON string value, or None if not set.
        """
        cursor = await self._conn.execute(
            "SELECT preference_value FROM user_preferences "
            "WHERE discord_user_id = ? AND preference_key = ?",
            (discord_user_id, key),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return row["preference_value"]

    async def get_all(self, discord_user_id: str) -> dict[str, str]:
        """Get all preferences for a user.

        Returns:
            Dict mapping preference keys to their JSON string values.
        """
        cursor = await self._conn.execute(
            "SELECT preference_key, preference_value FROM user_preferences "
            "WHERE discord_user_id = ?",
            (discord_user_id,),
        )
        rows = await cursor.fetchall()
        return {row["preference_key"]: row["preference_value"] for row in rows}

    async def set(self, discord_user_id: str, key: str, value: str) -> None:
        """Set a preference value (upserts)."""
        now = datetime.now(timezone.utc).isoformat()
        await self._conn.execute(
            """
            INSERT INTO user_preferences (id, discord_user_id, preference_key, preference_value, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(discord_user_id, preference_key)
            DO UPDATE SET preference_value = excluded.preference_value, updated_at = excluded.updated_at
            """,
            (str(uuid.uuid4()), discord_user_id, key, value, now),
        )
        await self._conn.commit()

    async def delete(self, discord_user_id: str, key: str) -> bool:
        """Delete a preference. Returns True if it existed."""
        cursor = await self._conn.execute(
            "DELETE FROM user_preferences WHERE discord_user_id = ? AND preference_key = ?",
            (discord_user_id, key),
        )
        await self._conn.commit()
        return cursor.rowcount > 0
