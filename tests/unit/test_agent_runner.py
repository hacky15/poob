"""Tests for AgentRunner tool-calling loop."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from agentic_scraper.agent.runner import AgentRunner
from agentic_scraper.storage.repositories.conversation_repo import ConversationRepository
from agentic_scraper.storage.repositories.preferences_repo import UserPreferencesRepository
from agentic_scraper.storage.repositories.watchlist_repo import WatchlistRepository


@pytest.fixture
async def prefs_repo(db_connection):
    return UserPreferencesRepository(db_connection)


@pytest.fixture
async def watchlist_repo(db_connection):
    return WatchlistRepository(db_connection)


@pytest.fixture
async def conversation_repo(db_connection):
    return ConversationRepository(db_connection)


@pytest.fixture
def deal_repo():
    repo = AsyncMock()
    repo.list_recent = AsyncMock(return_value=[])
    return repo


@pytest.fixture
def listing_repo():
    return AsyncMock()


@pytest.fixture
def scheduler():
    s = AsyncMock()
    s.trigger_now = AsyncMock()
    s.is_running = True
    s.is_paused = False
    s.last_scan_time = None
    s.next_scan_time = None
    return s


def _make_llm(*responses):
    """Create a mock LLM returning the given AIMessages in sequence.

    Usage::

        llm = _make_llm(
            AIMessage(content="Hi!"),                 # first call
            AIMessage(content="", tool_calls=[...]),  # second call
            AIMessage(content="Done."),               # third call
        )
    """
    llm = MagicMock()
    bound = AsyncMock()
    bound.ainvoke = AsyncMock(side_effect=list(responses))
    llm.bind_tools = MagicMock(return_value=bound)
    return llm


def _runner(llm, prefs_repo, watchlist_repo, conversation_repo,
            deal_repo, listing_repo, scheduler, **kwargs):
    return AgentRunner(
        llm=llm,
        prefs_repo=prefs_repo,
        watchlist_repo=watchlist_repo,
        conversation_repo=conversation_repo,
        deal_repo=deal_repo,
        listing_repo=listing_repo,
        scheduler=scheduler,
        **kwargs,
    )


class TestSimpleResponse:
    async def test_returns_text(
        self, prefs_repo, watchlist_repo, conversation_repo,
        deal_repo, listing_repo, scheduler, db_connection,
    ):
        llm = _make_llm(AIMessage(content="Hello! How can I help you find deals?"))
        runner = _runner(
            llm, prefs_repo, watchlist_repo, conversation_repo,
            deal_repo, listing_repo, scheduler,
        )

        result = await runner.run("Hi!", "user_1", "chan_1")
        assert "Hello" in result

    async def test_saves_user_and_assistant_messages(
        self, prefs_repo, watchlist_repo, conversation_repo,
        deal_repo, listing_repo, scheduler, db_connection,
    ):
        llm = _make_llm(AIMessage(content="Sure thing!"))
        runner = _runner(
            llm, prefs_repo, watchlist_repo, conversation_repo,
            deal_repo, listing_repo, scheduler,
        )

        await runner.run("Hello", "user_1", "chan_1")

        messages = await conversation_repo.load_recent("user_1", limit=10)
        assert len(messages) == 2
        assert messages[0]["role"] == "user"
        assert messages[0]["content"] == "Hello"
        assert messages[1]["role"] == "assistant"
        assert messages[1]["content"] == "Sure thing!"


class TestToolCallingLoop:
    async def test_executes_tool_and_returns_final_text(
        self, prefs_repo, watchlist_repo, conversation_repo,
        deal_repo, listing_repo, scheduler, db_connection,
    ):
        """LLM calls a tool, gets result, then produces text."""
        tool_call = AIMessage(
            content="",
            tool_calls=[{"name": "show_wishlist", "args": {}, "id": "call_1"}],
        )
        final = AIMessage(content="Your wishlist is currently empty.")

        llm = _make_llm(tool_call, final)
        runner = _runner(
            llm, prefs_repo, watchlist_repo, conversation_repo,
            deal_repo, listing_repo, scheduler,
        )

        result = await runner.run("What's on my list?", "user_1", "chan_1")
        assert "empty" in result.lower()

    async def test_multiple_tool_calls(
        self, prefs_repo, watchlist_repo, conversation_repo,
        deal_repo, listing_repo, scheduler, db_connection,
    ):
        """LLM calls two tools sequentially before producing text."""
        call_1 = AIMessage(
            content="",
            tool_calls=[{
                "name": "add_to_wishlist",
                "args": {"item_name": "rug", "max_price": 100},
                "id": "call_1",
            }],
        )
        call_2 = AIMessage(
            content="",
            tool_calls=[{"name": "show_wishlist", "args": {}, "id": "call_2"}],
        )
        final = AIMessage(content="Added rug and here's your list.")

        llm = _make_llm(call_1, call_2, final)
        runner = _runner(
            llm, prefs_repo, watchlist_repo, conversation_repo,
            deal_repo, listing_repo, scheduler,
        )

        result = await runner.run("Add a rug under $100 and show my list", "user_1", "chan_1")
        assert isinstance(result, str)
        assert len(result) > 0

    async def test_max_iterations_stops_loop(
        self, prefs_repo, watchlist_repo, conversation_repo,
        deal_repo, listing_repo, scheduler, db_connection,
    ):
        """Loop stops at max_iterations even if LLM keeps calling tools."""
        infinite_call = AIMessage(
            content="",
            tool_calls=[{"name": "show_wishlist", "args": {}, "id": "call_x"}],
        )
        llm = MagicMock()
        bound = AsyncMock()
        bound.ainvoke = AsyncMock(return_value=infinite_call)
        llm.bind_tools = MagicMock(return_value=bound)

        runner = _runner(
            llm, prefs_repo, watchlist_repo, conversation_repo,
            deal_repo, listing_repo, scheduler,
            max_iterations=3,
        )

        result = await runner.run("loop forever", "user_1", "chan_1")
        assert isinstance(result, str)
        assert len(result) > 0
        # Should have called ainvoke exactly 3 times
        assert bound.ainvoke.call_count == 3


class TestConversationHistory:
    async def test_loads_history_into_context(
        self, prefs_repo, watchlist_repo, conversation_repo,
        deal_repo, listing_repo, scheduler, db_connection,
    ):
        await conversation_repo.save_message("user_1", "user", "I like rugs")
        await conversation_repo.save_message("user_1", "assistant", "Noted!")

        llm = _make_llm(AIMessage(content="Got it."))
        runner = _runner(
            llm, prefs_repo, watchlist_repo, conversation_repo,
            deal_repo, listing_repo, scheduler,
        )

        await runner.run("What did I say?", "user_1", "chan_1")

        bound = llm.bind_tools.return_value
        call_messages = bound.ainvoke.call_args[0][0]
        # system + 2 history + 1 new user = 4
        assert len(call_messages) >= 4
        assert isinstance(call_messages[0], SystemMessage)
        assert isinstance(call_messages[1], HumanMessage)
        assert call_messages[1].content == "I like rugs"

    async def test_trims_after_save(
        self, prefs_repo, watchlist_repo, conversation_repo,
        deal_repo, listing_repo, scheduler, db_connection,
    ):
        for i in range(55):
            await conversation_repo.save_message("user_1", "user", f"msg {i}")

        llm = _make_llm(AIMessage(content="OK."))
        runner = _runner(
            llm, prefs_repo, watchlist_repo, conversation_repo,
            deal_repo, listing_repo, scheduler,
        )

        await runner.run("one more", "user_1", "chan_1")

        all_msgs = await conversation_repo.load_recent("user_1", limit=200)
        # 55 old + 2 new = 57, trimmed to 50
        assert len(all_msgs) <= 50


class TestSystemPrompt:
    async def test_includes_wishlist(
        self, prefs_repo, watchlist_repo, conversation_repo,
        deal_repo, listing_repo, scheduler, db_connection,
    ):
        await prefs_repo.set(
            "user_1", "wishlist",
            json.dumps([{"name": "espresso machine", "max_price": 200, "priority": "high"}]),
        )

        llm = _make_llm(AIMessage(content="I see your list."))
        runner = _runner(
            llm, prefs_repo, watchlist_repo, conversation_repo,
            deal_repo, listing_repo, scheduler,
        )

        await runner.run("Hi", "user_1", "chan_1")

        bound = llm.bind_tools.return_value
        system_msg = bound.ainvoke.call_args[0][0][0]
        assert isinstance(system_msg, SystemMessage)
        assert "espresso machine" in system_msg.content

    async def test_includes_location(
        self, prefs_repo, watchlist_repo, conversation_repo,
        deal_repo, listing_repo, scheduler, db_connection,
    ):
        await prefs_repo.set(
            "user_1", "location",
            json.dumps({"city": "Portland, OR", "radius_miles": 25}),
        )

        llm = _make_llm(AIMessage(content="OK."))
        runner = _runner(
            llm, prefs_repo, watchlist_repo, conversation_repo,
            deal_repo, listing_repo, scheduler,
        )

        await runner.run("Hi", "user_1", "chan_1")

        bound = llm.bind_tools.return_value
        system_msg = bound.ainvoke.call_args[0][0][0]
        assert "Portland" in system_msg.content

    async def test_includes_search_priorities(
        self, prefs_repo, watchlist_repo, conversation_repo,
        deal_repo, listing_repo, scheduler, db_connection,
    ):
        await prefs_repo.set(
            "user_1", "search_priorities",
            json.dumps({"just_listed_first": True, "desperate_seller_detection": True}),
        )

        llm = _make_llm(AIMessage(content="OK."))
        runner = _runner(
            llm, prefs_repo, watchlist_repo, conversation_repo,
            deal_repo, listing_repo, scheduler,
        )

        await runner.run("Hi", "user_1", "chan_1")

        bound = llm.bind_tools.return_value
        system_msg = bound.ainvoke.call_args[0][0][0]
        assert "just-listed" in system_msg.content.lower() or "just_listed" in system_msg.content
