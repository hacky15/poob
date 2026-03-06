"""Repository for persisting user feedback on deal notifications."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from agentic_scraper.storage.models import DealFeedback

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
            """INSERT INTO deal_feedback (id, deal_id, discord_user_id, feedback_type, created_at)
            VALUES (?, ?, ?, ?, ?)""",
            (
                feedback.id,
                feedback.deal_id,
                feedback.discord_user_id,
                feedback.feedback_type,
                feedback.created_at.isoformat(),
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

    @staticmethod
    def _row_to_feedback(row: object) -> DealFeedback:
        """Convert a database row to a DealFeedback instance."""
        return DealFeedback(
            id=row["id"],
            deal_id=row["deal_id"],
            discord_user_id=row["discord_user_id"],
            feedback_type=row["feedback_type"],
            created_at=datetime.fromisoformat(row["created_at"]).replace(
                tzinfo=timezone.utc
            ),
        )
