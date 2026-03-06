"""Repository for WatchItem CRUD operations."""

from __future__ import annotations

import json
import uuid
from datetime import datetime

import aiosqlite

from agentic_scraper.storage.models import WatchItem


class WatchlistRepository:
    """CRUD operations for watch items (user's priority interests)."""

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def save(self, item: WatchItem) -> WatchItem:
        """Insert a watch item. Assigns an ID if not set."""
        if item.id is None:
            item.id = str(uuid.uuid4())

        await self._conn.execute(
            """
            INSERT OR REPLACE INTO watch_items
                (id, interest, max_price, location, radius_miles, category,
                 sites, is_active, created_at, discord_user_id, discord_channel_id,
                 notification_threshold, notes, search_configs)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item.id,
                item.interest,
                item.max_price,
                item.location,
                item.radius_miles,
                item.category,
                json.dumps(item.sites),
                int(item.is_active),
                item.created_at.isoformat(),
                item.discord_user_id,
                item.discord_channel_id,
                item.notification_threshold,
                item.notes,
                json.dumps(item.search_configs),
            ),
        )
        await self._conn.commit()
        return item

    async def get(self, item_id: str) -> WatchItem | None:
        """Fetch a watch item by ID."""
        cursor = await self._conn.execute(
            "SELECT * FROM watch_items WHERE id = ?", (item_id,)
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return self._row_to_watch_item(row)

    async def list_for_user(self, discord_user_id: str) -> list[WatchItem]:
        """List all watch items for a specific Discord user."""
        cursor = await self._conn.execute(
            "SELECT * FROM watch_items WHERE discord_user_id = ? ORDER BY created_at DESC",
            (discord_user_id,),
        )
        rows = await cursor.fetchall()
        return [self._row_to_watch_item(row) for row in rows]

    async def list_active(self) -> list[WatchItem]:
        """List all active watch items across all users."""
        cursor = await self._conn.execute(
            "SELECT * FROM watch_items WHERE is_active = 1 ORDER BY created_at DESC"
        )
        rows = await cursor.fetchall()
        return [self._row_to_watch_item(row) for row in rows]

    async def deactivate(self, item_id: str) -> bool:
        """Deactivate a watch item (set is_active=0). Returns True if updated."""
        cursor = await self._conn.execute(
            "UPDATE watch_items SET is_active = 0 WHERE id = ?",
            (item_id,),
        )
        await self._conn.commit()
        return cursor.rowcount > 0

    async def deactivate_all_for_user(self, discord_user_id: str) -> int:
        """Deactivate all active watch items for a user. Returns count deactivated."""
        cursor = await self._conn.execute(
            "UPDATE watch_items SET is_active = 0 WHERE discord_user_id = ? AND is_active = 1",
            (discord_user_id,),
        )
        await self._conn.commit()
        return cursor.rowcount

    async def delete(self, item_id: str, discord_user_id: str) -> bool:
        """Delete a watch item. Only the owning user can delete. Returns True if deleted."""
        cursor = await self._conn.execute(
            "DELETE FROM watch_items WHERE id = ? AND discord_user_id = ?",
            (item_id, discord_user_id),
        )
        await self._conn.commit()
        return cursor.rowcount > 0

    @staticmethod
    def _row_to_watch_item(row: aiosqlite.Row) -> WatchItem:
        """Convert a database row to a WatchItem dataclass."""
        return WatchItem(
            id=row["id"],
            interest=row["interest"],
            max_price=row["max_price"],
            location=row["location"],
            radius_miles=row["radius_miles"],
            category=row["category"],
            sites=json.loads(row["sites"]),
            is_active=bool(row["is_active"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            discord_user_id=row["discord_user_id"],
            discord_channel_id=row["discord_channel_id"],
            notification_threshold=row["notification_threshold"] or "good",
            notes=row["notes"] if "notes" in row.keys() else "",
            search_configs=json.loads(row["search_configs"]) if "search_configs" in row.keys() else [],
        )
