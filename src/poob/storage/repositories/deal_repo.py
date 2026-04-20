"""Repository for Deal CRUD operations."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import aiosqlite

from poob.storage.models import Deal, DealScore


class DealRepository:
    """CRUD operations for deals (listings matched or flagged)."""

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def save(self, deal: Deal) -> Deal:
        """Insert a deal. Assigns an ID if not set."""
        if deal.id is None:
            deal.id = str(uuid.uuid4())

        await self._conn.execute(
            """
            INSERT INTO deals
                (id, listing_id, watch_item_id, score, estimated_market_price,
                 discount_pct, llm_reasoning, provenance_json,
                 notified, notified_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                deal.id,
                deal.listing_id,
                deal.watch_item_id,
                deal.score.value,
                deal.estimated_market_price,
                deal.discount_pct,
                deal.llm_reasoning,
                deal.provenance_json,
                int(deal.notified),
                deal.notified_at.isoformat() if deal.notified_at else None,
                deal.created_at.isoformat(),
            ),
        )
        await self._conn.commit()
        return deal

    async def get(self, deal_id: str) -> Deal | None:
        """Fetch a deal by ID."""
        cursor = await self._conn.execute(
            "SELECT * FROM deals WHERE id = ?", (deal_id,)
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return self._row_to_deal(row)

    async def list_recent(self, limit: int = 20) -> list[Deal]:
        """List the most recent deals."""
        cursor = await self._conn.execute(
            "SELECT * FROM deals ORDER BY created_at DESC LIMIT ?", (limit,)
        )
        rows = await cursor.fetchall()
        return [self._row_to_deal(row) for row in rows]

    async def list_unnotified(self) -> list[Deal]:
        """List deals that haven't been sent to Discord yet."""
        cursor = await self._conn.execute(
            "SELECT * FROM deals WHERE notified = 0 ORDER BY created_at ASC"
        )
        rows = await cursor.fetchall()
        return [self._row_to_deal(row) for row in rows]

    async def mark_notified(self, deal_id: str) -> None:
        """Mark a deal as notified."""
        now = datetime.now(timezone.utc).isoformat()
        await self._conn.execute(
            "UPDATE deals SET notified = 1, notified_at = ? WHERE id = ?",
            (now, deal_id),
        )
        await self._conn.commit()

    async def update(self, deal: Deal) -> None:
        """Update an existing deal."""
        await self._conn.execute(
            """
            UPDATE deals SET
                listing_id = ?, watch_item_id = ?, score = ?,
                estimated_market_price = ?, discount_pct = ?,
                llm_reasoning = ?, provenance_json = ?,
                notified = ?, notified_at = ?
            WHERE id = ?
            """,
            (
                deal.listing_id,
                deal.watch_item_id,
                deal.score.value,
                deal.estimated_market_price,
                deal.discount_pct,
                deal.llm_reasoning,
                deal.provenance_json,
                int(deal.notified),
                deal.notified_at.isoformat() if deal.notified_at else None,
                deal.id,
            ),
        )
        await self._conn.commit()

    @staticmethod
    def _row_to_deal(row: aiosqlite.Row) -> Deal:
        """Convert a database row to a Deal dataclass."""
        # provenance_json may not exist in older DBs before migration runs
        try:
            provenance_json = row["provenance_json"] or ""
        except (IndexError, KeyError):
            provenance_json = ""

        return Deal(
            id=row["id"],
            listing_id=row["listing_id"],
            watch_item_id=row["watch_item_id"],
            score=DealScore(row["score"]),
            estimated_market_price=row["estimated_market_price"],
            discount_pct=row["discount_pct"],
            llm_reasoning=row["llm_reasoning"],
            provenance_json=provenance_json,
            notified=bool(row["notified"]),
            notified_at=(
                datetime.fromisoformat(row["notified_at"]) if row["notified_at"] else None
            ),
            created_at=datetime.fromisoformat(row["created_at"]),
        )
