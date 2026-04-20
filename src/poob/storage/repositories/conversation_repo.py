"""Repository for conversation message history."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import aiosqlite


class ConversationRepository:
    """Per-user conversation history for the conversational agent.

    Stores user/assistant/tool messages with timestamps.
    Supports loading recent messages for LLM context injection
    and trimming old messages to bound storage.

    Args:
        conn: aiosqlite database connection.
    """

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def save_message(
        self,
        discord_user_id: str,
        role: str,
        content: str,
        tool_call_id: str | None = None,
    ) -> None:
        """Save a conversation message.

        Args:
            discord_user_id: The Discord user this message belongs to.
            role: Message role ('user', 'assistant', 'tool').
            content: Message content.
            tool_call_id: Optional tool call ID for tool result messages.
        """
        now = datetime.now(timezone.utc).isoformat()
        await self._conn.execute(
            """
            INSERT INTO conversation_messages
                (id, discord_user_id, role, content, tool_call_id, timestamp)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (str(uuid.uuid4()), discord_user_id, role, content, tool_call_id, now),
        )
        await self._conn.commit()

    async def load_recent(
        self, discord_user_id: str, limit: int = 20
    ) -> list[dict[str, str | None]]:
        """Load the most recent messages for a user.

        Args:
            discord_user_id: The Discord user.
            limit: Maximum number of messages to return.

        Returns:
            List of message dicts with 'role', 'content', 'tool_call_id' keys,
            ordered oldest-first.
        """
        cursor = await self._conn.execute(
            """
            SELECT role, content, tool_call_id FROM (
                SELECT role, content, tool_call_id, timestamp
                FROM conversation_messages
                WHERE discord_user_id = ?
                ORDER BY timestamp DESC
                LIMIT ?
            ) ORDER BY timestamp ASC
            """,
            (discord_user_id, limit),
        )
        rows = await cursor.fetchall()
        return [
            {
                "role": row["role"],
                "content": row["content"],
                "tool_call_id": row["tool_call_id"],
            }
            for row in rows
        ]

    async def trim(self, discord_user_id: str, keep: int = 50) -> None:
        """Delete old messages beyond the retention limit.

        Keeps the most recent `keep` messages, deletes the rest.

        Args:
            discord_user_id: The Discord user.
            keep: Number of most recent messages to retain.
        """
        await self._conn.execute(
            """
            DELETE FROM conversation_messages
            WHERE discord_user_id = ?
            AND id NOT IN (
                SELECT id FROM conversation_messages
                WHERE discord_user_id = ?
                ORDER BY timestamp DESC
                LIMIT ?
            )
            """,
            (discord_user_id, discord_user_id, keep),
        )
        await self._conn.commit()
