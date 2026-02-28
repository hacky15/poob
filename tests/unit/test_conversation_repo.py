"""Tests for ConversationRepository."""

from __future__ import annotations

import pytest

from agentic_scraper.storage.repositories.conversation_repo import ConversationRepository


class TestConversationRepository:
    """CRUD tests for conversation messages using in-memory SQLite."""

    async def test_save_and_load(self, db_connection):
        repo = ConversationRepository(db_connection)
        await repo.save_message("user_1", "user", "Hello!")
        await repo.save_message("user_1", "assistant", "Hi there!")

        messages = await repo.load_recent("user_1", limit=10)
        assert len(messages) == 2
        assert messages[0]["role"] == "user"
        assert messages[0]["content"] == "Hello!"
        assert messages[1]["role"] == "assistant"
        assert messages[1]["content"] == "Hi there!"

    async def test_load_recent_ordering(self, db_connection):
        repo = ConversationRepository(db_connection)
        await repo.save_message("user_1", "user", "first")
        await repo.save_message("user_1", "assistant", "second")
        await repo.save_message("user_1", "user", "third")

        messages = await repo.load_recent("user_1", limit=10)
        contents = [m["content"] for m in messages]
        assert contents == ["first", "second", "third"]

    async def test_load_recent_respects_limit(self, db_connection):
        repo = ConversationRepository(db_connection)
        for i in range(10):
            await repo.save_message("user_1", "user", f"msg {i}")

        messages = await repo.load_recent("user_1", limit=3)
        assert len(messages) == 3
        # Should get the 3 most recent
        contents = [m["content"] for m in messages]
        assert contents == ["msg 7", "msg 8", "msg 9"]

    async def test_different_users_isolated(self, db_connection):
        repo = ConversationRepository(db_connection)
        await repo.save_message("user_a", "user", "A's message")
        await repo.save_message("user_b", "user", "B's message")

        a_msgs = await repo.load_recent("user_a", limit=10)
        b_msgs = await repo.load_recent("user_b", limit=10)
        assert len(a_msgs) == 1
        assert len(b_msgs) == 1
        assert a_msgs[0]["content"] == "A's message"
        assert b_msgs[0]["content"] == "B's message"

    async def test_trim_old_messages(self, db_connection):
        repo = ConversationRepository(db_connection)
        for i in range(60):
            await repo.save_message("user_1", "user", f"msg {i}")

        await repo.trim("user_1", keep=50)

        messages = await repo.load_recent("user_1", limit=100)
        assert len(messages) == 50
        # Oldest messages trimmed, newest kept
        assert messages[0]["content"] == "msg 10"
        assert messages[-1]["content"] == "msg 59"

    async def test_trim_noop_when_under_limit(self, db_connection):
        repo = ConversationRepository(db_connection)
        await repo.save_message("user_1", "user", "only one")

        await repo.trim("user_1", keep=50)

        messages = await repo.load_recent("user_1", limit=100)
        assert len(messages) == 1

    async def test_load_empty_returns_empty_list(self, db_connection):
        repo = ConversationRepository(db_connection)
        messages = await repo.load_recent("user_nobody", limit=10)
        assert messages == []

    async def test_save_with_tool_call_id(self, db_connection):
        repo = ConversationRepository(db_connection)
        await repo.save_message(
            "user_1", "tool", "tool result", tool_call_id="call_123"
        )

        messages = await repo.load_recent("user_1", limit=10)
        assert len(messages) == 1
        assert messages[0]["role"] == "tool"
        assert messages[0]["tool_call_id"] == "call_123"
