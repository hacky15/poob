"""Repository for persisting user feedback on deal notifications."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from poob.storage.models import DealFeedback

if TYPE_CHECKING:
    import aiosqlite


class FeedbackRepository:
    """Persist and query user feedback on deal notifications.

    Feedback comes from Discord reaction buttons on deal embeds:
    - claimed: User bought the item (positive signal)
    - overpriced: Not actually a deal (false positive signal)
    - scam: Listing is fraudulent (strong negative signal)
    - not_interested: Not what user wants (preference signal)

    Args:
        conn: aiosqlite database connection.
    """

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def save(self, feedback: DealFeedback) -> DealFeedback:
        """Save a feedback entry.

        Args:
            feedback: The feedback to persist.

        Returns:
            The saved feedback with generated ID.
        """
        if not feedback.id:
            feedback.id = str(uuid.uuid4())

        await self._conn.execute(
            """INSERT INTO deal_feedback
            (id, deal_id, discord_user_id, feedback_type, created_at,
             listing_title, listing_price, vlm_output, deal_quality,
             estimated_value, provider_used, enrichment_data)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                feedback.id,
                feedback.deal_id,
                feedback.discord_user_id,
                feedback.feedback_type,
                feedback.created_at.isoformat(),
                feedback.listing_title,
                feedback.listing_price,
                json.dumps(feedback.vlm_output) if feedback.vlm_output else "{}",
                feedback.deal_quality,
                feedback.estimated_value,
                feedback.provider_used,
                json.dumps(feedback.enrichment_data) if feedback.enrichment_data else "{}",
            ),
        )
        await self._conn.commit()
        return feedback

    async def get_for_deal(self, deal_id: str) -> list[DealFeedback]:
        """Get all feedback entries for a specific deal.

        Args:
            deal_id: The deal to get feedback for.

        Returns:
            List of feedback entries.
        """
        cursor = await self._conn.execute(
            "SELECT * FROM deal_feedback WHERE deal_id = ? ORDER BY created_at DESC",
            (deal_id,),
        )
        rows = await cursor.fetchall()
        return [self._row_to_feedback(row) for row in rows]

    async def get_for_user(
        self, discord_user_id: str, limit: int = 50
    ) -> list[DealFeedback]:
        """Get recent feedback from a specific user.

        Args:
            discord_user_id: The Discord user ID.
            limit: Maximum number of entries to return.

        Returns:
            List of feedback entries, newest first.
        """
        cursor = await self._conn.execute(
            """SELECT * FROM deal_feedback
            WHERE discord_user_id = ?
            ORDER BY created_at DESC
            LIMIT ?""",
            (discord_user_id, limit),
        )
        rows = await cursor.fetchall()
        return [self._row_to_feedback(row) for row in rows]

    async def get_stats(self, discord_user_id: str) -> dict:
        """Get feedback statistics for a user.

        Args:
            discord_user_id: The Discord user ID.

        Returns:
            Dict with counts per feedback type.
        """
        cursor = await self._conn.execute(
            """SELECT feedback_type, COUNT(*) as count
            FROM deal_feedback
            WHERE discord_user_id = ?
            GROUP BY feedback_type""",
            (discord_user_id,),
        )
        rows = await cursor.fetchall()
        stats: dict[str, int] = {}
        for row in rows:
            stats[row["feedback_type"]] = row["count"]
        return stats

    async def exists(self, deal_id: str, discord_user_id: str) -> bool:
        """Check if a user has already given feedback on a deal.

        Args:
            deal_id: The deal ID.
            discord_user_id: The Discord user ID.

        Returns:
            True if feedback already exists.
        """
        cursor = await self._conn.execute(
            "SELECT 1 FROM deal_feedback WHERE deal_id = ? AND discord_user_id = ? LIMIT 1",
            (deal_id, discord_user_id),
        )
        return await cursor.fetchone() is not None

    async def get_enriched_feedback(
        self, limit: int = 100, feedback_types: list[str] | None = None
    ) -> list[DealFeedback]:
        """Get enriched feedback entries for the learning pipeline.

        Prioritizes false positive examples (overpriced/scam) which are
        most valuable for improving VLM accuracy.

        Args:
            limit: Maximum entries to return.
            feedback_types: Filter by specific types. None = all types.

        Returns:
            List of enriched feedback entries.
        """
        if feedback_types:
            placeholders = ",".join("?" for _ in feedback_types)
            query = f"""SELECT * FROM deal_feedback
                WHERE feedback_type IN ({placeholders})
                AND listing_title IS NOT NULL AND listing_title != ''
                ORDER BY created_at DESC LIMIT ?"""
            params = (*feedback_types, limit)
        else:
            query = """SELECT * FROM deal_feedback
                WHERE listing_title IS NOT NULL AND listing_title != ''
                ORDER BY created_at DESC LIMIT ?"""
            params = (limit,)

        cursor = await self._conn.execute(query, params)
        rows = await cursor.fetchall()
        return [self._row_to_feedback(row) for row in rows]

    @staticmethod
    def _row_to_feedback(row: object) -> DealFeedback:
        """Convert a database row to a DealFeedback instance."""
        vlm_output_raw = row["vlm_output"] if "vlm_output" in row.keys() else "{}"
        enrichment_raw = row["enrichment_data"] if "enrichment_data" in row.keys() else "{}"

        return DealFeedback(
            id=row["id"],
            deal_id=row["deal_id"],
            discord_user_id=row["discord_user_id"],
            feedback_type=row["feedback_type"],
            created_at=datetime.fromisoformat(row["created_at"]).replace(
                tzinfo=timezone.utc
            ),
            listing_title=row["listing_title"] if "listing_title" in row.keys() else "",
            listing_price=float(row["listing_price"]) if "listing_price" in row.keys() and row["listing_price"] else 0.0,
            vlm_output=json.loads(vlm_output_raw) if vlm_output_raw else {},
            deal_quality=row["deal_quality"] if "deal_quality" in row.keys() else "",
            estimated_value=float(row["estimated_value"]) if "estimated_value" in row.keys() and row["estimated_value"] else 0.0,
            provider_used=row["provider_used"] if "provider_used" in row.keys() else "",
            enrichment_data=json.loads(enrichment_raw) if enrichment_raw else {},
        )
