"""Repository for ExclusionItem CRUD operations."""

from __future__ import annotations

import uuid
from datetime import datetime

import aiosqlite

from poob.storage.models import ExclusionItem


class ExclusionRepository:
    """CRUD operations for exclusion items (things users don't want to see)."""

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def save(self, item: ExclusionItem) -> ExclusionItem:
        """Insert an exclusion item. Assigns an ID if not set."""
        if item.id is None:
            item.id = str(uuid.uuid4())

        await self._conn.execute(
            """
            INSERT INTO exclusion_items (id, keyword, discord_user_id, is_active, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                item.id,
                item.keyword,
                item.discord_user_id,
                int(item.is_active),
                item.created_at.isoformat(),
            ),
        )
        await self._conn.commit()
        return item

    async def list_for_user(self, discord_user_id: str) -> list[ExclusionItem]:
        """List all active exclusion items for a specific Discord user."""
        cursor = await self._conn.execute(
            "SELECT * FROM exclusion_items WHERE discord_user_id = ? AND is_active = 1"
            " ORDER BY created_at DESC",
            (discord_user_id,),
        )
        rows = await cursor.fetchall()
        return [self._row_to_item(row) for row in rows]

    async def list_all_active(self) -> list[ExclusionItem]:
        """List all active exclusion items across all users."""
        cursor = await self._conn.execute(
            "SELECT * FROM exclusion_items WHERE is_active = 1 ORDER BY created_at DESC"
        )
        rows = await cursor.fetchall()
        return [self._row_to_item(row) for row in rows]

    async def deactivate(self, item_id: str) -> bool:
        """Deactivate an exclusion item. Returns True if updated."""
        cursor = await self._conn.execute(
            "UPDATE exclusion_items SET is_active = 0 WHERE id = ?",
            (item_id,),
        )
        await self._conn.commit()
        return cursor.rowcount > 0

    async def delete_for_user(
        self, keyword: str, discord_user_id: str
    ) -> bool:
        """Delete an exclusion item by keyword for a user. Returns True if deleted."""
        cursor = await self._conn.execute(
            "DELETE FROM exclusion_items WHERE LOWER(keyword) = LOWER(?)"
            " AND discord_user_id = ?",
            (keyword, discord_user_id),
        )
        await self._conn.commit()
        return cursor.rowcount > 0

    @staticmethod
    def _row_to_item(row: aiosqlite.Row) -> ExclusionItem:
        """Convert a database row to an ExclusionItem dataclass."""
        return ExclusionItem(
            id=row["id"],
            keyword=row["keyword"],
            discord_user_id=row["discord_user_id"],
            is_active=bool(row["is_active"]),
            created_at=datetime.fromisoformat(row["created_at"]),
        )
