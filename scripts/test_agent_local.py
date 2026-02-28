"""Quick standalone test of the agent runner against a live LLM.

Usage:
    python scripts/test_agent_local.py

Tests the full agent pipeline without Discord — sends a message directly
to the AgentRunner and prints the response.
"""

from __future__ import annotations

import asyncio

from agentic_scraper.config import AppConfig


async def main() -> None:
    import aiosqlite

    from agentic_scraper.agent.runner import AgentRunner
    from agentic_scraper.llm.provider import create_cloud_provider, create_llm_provider
    from agentic_scraper.storage.database import init_schema
    from agentic_scraper.storage.repositories.conversation_repo import ConversationRepository
    from agentic_scraper.storage.repositories.deal_repo import DealRepository
    from agentic_scraper.storage.repositories.listing_repo import ListingRepository
    from agentic_scraper.storage.repositories.preferences_repo import UserPreferencesRepository
    from agentic_scraper.storage.repositories.watchlist_repo import WatchlistRepository

    config = AppConfig()

    # Use in-memory DB for testing
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await init_schema(conn)

    prefs_repo = UserPreferencesRepository(conn)
    watchlist_repo = WatchlistRepository(conn)
    conversation_repo = ConversationRepository(conn)
    deal_repo = DealRepository(conn)
    listing_repo = ListingRepository(conn)

    # Pick the agent brain LLM
    cloud_provider = create_cloud_provider(config)
    langchain_provider = create_llm_provider(config)

    if config.agent_llm_provider == "gemini" and cloud_provider and cloud_provider.is_available():
        llm = cloud_provider.chat_model
        print(f"Using cloud LLM: {cloud_provider.model_name}")
    else:
        llm = langchain_provider.chat_model
        print(f"Using local LLM: {langchain_provider.model_name}")

    # Quick test: can the LLM respond at all?
    print("\n--- Test 1: Raw LLM call (no tools) ---")
    try:
        from langchain_core.messages import HumanMessage
        response = await llm.ainvoke([HumanMessage(content="Say hello in one sentence.")])
        print(f"  Response: {response.content}")
    except Exception as e:
        print(f"  ERROR: {e}")
        return

    # Test: can the LLM handle tool binding?
    print("\n--- Test 2: LLM with tools bound ---")
    try:
        from agentic_scraper.agent.tools import build_tools

        # Fake scheduler
        class FakeScheduler:
            is_running = True
            is_paused = False
            last_scan_time = None
            next_scan_time = None
            async def trigger_now(self): pass

        tools = build_tools(
            discord_user_id="test_user",
            discord_channel_id="test_chan",
            prefs_repo=prefs_repo,
            watchlist_repo=watchlist_repo,
            deal_repo=deal_repo,
            listing_repo=listing_repo,
            scheduler=FakeScheduler(),
        )
        bound = llm.bind_tools(tools)
        print(f"  Tools bound: {[t.name for t in tools]}")

        from langchain_core.messages import SystemMessage
        response = await bound.ainvoke([
            SystemMessage(content="You are a deal-hunting assistant."),
            HumanMessage(content="What can you do?"),
        ])
        print(f"  Response content: {response.content}")
        print(f"  Tool calls: {response.tool_calls}")
    except Exception as e:
        print(f"  ERROR: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        return

    # Test: full agent runner
    print("\n--- Test 3: Full AgentRunner ---")
    try:
        runner = AgentRunner(
            llm=llm,
            prefs_repo=prefs_repo,
            watchlist_repo=watchlist_repo,
            conversation_repo=conversation_repo,
            deal_repo=deal_repo,
            listing_repo=listing_repo,
            scheduler=FakeScheduler(),
            max_iterations=5,
        )
        result = await runner.run(
            "Hi! I'm looking for a coffee table under $100. Can you help?",
            "test_user",
            "test_chan",
        )
        print(f"  Agent response: {result}")
    except Exception as e:
        print(f"  ERROR: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()

    await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
