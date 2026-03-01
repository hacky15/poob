"""Agent runner: tool-calling loop with conversation memory."""

from __future__ import annotations

import json
from typing import Any

import structlog
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from agentic_scraper.agent.prompts import build_system_prompt
from agentic_scraper.agent.tools import build_tools
from agentic_scraper.storage.repositories.conversation_repo import ConversationRepository
from agentic_scraper.storage.repositories.preferences_repo import UserPreferencesRepository
from agentic_scraper.storage.repositories.watchlist_repo import WatchlistRepository

log = structlog.get_logger()


class AgentRunner:
    """Runs the conversational agent loop for a single user turn.

    For each user message:
    1. Loads conversation history and user preferences.
    2. Builds a dynamic system prompt with preferences injected.
    3. Binds tools scoped to the user.
    4. Runs a tool-calling loop until the LLM produces a text response
       or max_iterations is reached.
    5. Saves the conversation to the database.

    Args:
        llm: LangChain-compatible chat model (the agent brain).
        prefs_repo: User preferences repository.
        watchlist_repo: Watchlist repository.
        conversation_repo: Conversation history repository.
        deal_repo: Deal repository.
        listing_repo: Listing repository.
        scheduler: Scan scheduler instance.
        max_iterations: Maximum tool-calling rounds before forcing a stop.
    """

    def __init__(
        self,
        llm: BaseChatModel,
        prefs_repo: UserPreferencesRepository,
        watchlist_repo: WatchlistRepository,
        conversation_repo: ConversationRepository,
        deal_repo: Any,
        listing_repo: Any,
        scheduler: Any,
        max_iterations: int = 10,
        scan_log_repo: Any = None,
    ) -> None:
        self._llm = llm
        self._prefs_repo = prefs_repo
        self._watchlist_repo = watchlist_repo
        self._conversation_repo = conversation_repo
        self._deal_repo = deal_repo
        self._listing_repo = listing_repo
        self._scheduler = scheduler
        self._max_iterations = max_iterations
        self._scan_log_repo = scan_log_repo

    async def run(
        self,
        user_message: str,
        discord_user_id: str,
        discord_channel_id: str,
    ) -> str:
        """Process a user message and return the agent's response.

        Args:
            user_message: The user's natural-language message.
            discord_user_id: Discord user ID for scoping tools and history.
            discord_channel_id: Discord channel ID.

        Returns:
            The agent's text response.
        """
        # 1. Load user preferences for prompt injection
        system_prompt = await self._build_system_prompt(discord_user_id)

        # 2. Load conversation history
        history = await self._load_history(discord_user_id)

        # 3. Build tools scoped to this user
        tools = build_tools(
            discord_user_id=discord_user_id,
            discord_channel_id=discord_channel_id,
            prefs_repo=self._prefs_repo,
            watchlist_repo=self._watchlist_repo,
            deal_repo=self._deal_repo,
            listing_repo=self._listing_repo,
            scheduler=self._scheduler,
            scan_log_repo=self._scan_log_repo,
        )
        tools_by_name = {t.name: t for t in tools}

        # 4. Prepare messages
        messages: list[BaseMessage] = [SystemMessage(content=system_prompt)]
        messages.extend(history)
        messages.append(HumanMessage(content=user_message))

        # 5. Bind tools to LLM
        bound_llm = self._llm.bind_tools(tools)

        # 6. Tool-calling loop
        response_text, tools_were_called = await self._run_loop(
            bound_llm, messages, tools_by_name,
        )

        # 7. Save conversation
        # Mark tool-call turns so they don't pollute history.
        # When the model sees old AIMessages with plain text that *should* have
        # been tool calls, it mimics that pattern instead of calling tools.
        # Marking with tool_call_id="__TOOL_TURN__" lets _load_history skip them.
        tool_marker = "__TOOL_TURN__" if tools_were_called else None
        await self._conversation_repo.save_message(
            discord_user_id, "user", user_message, tool_call_id=tool_marker,
        )
        await self._conversation_repo.save_message(
            discord_user_id, "assistant", response_text, tool_call_id=tool_marker,
        )

        # 8. Trim old messages
        await self._conversation_repo.trim(discord_user_id, keep=50)

        return response_text

    async def _run_loop(
        self,
        bound_llm: Any,
        messages: list[BaseMessage],
        tools_by_name: dict,
    ) -> tuple[str, bool]:
        """Execute the ReAct-style tool-calling loop.

        Returns:
            Tuple of (final text response, whether any tools were called).
        """
        any_tools_called = False

        for iteration in range(self._max_iterations):
            response: AIMessage = await bound_llm.ainvoke(messages)

            log.info(
                "agent.llm_response",
                iteration=iteration,
                has_tool_calls=bool(response.tool_calls),
                tool_calls=[tc.get("name", "?") for tc in (response.tool_calls or [])],
                content_type=type(response.content).__name__,
                content_preview=str(response.content)[:120],
            )

            # If no tool calls, we have our final answer
            if not response.tool_calls:
                return _extract_text(response.content), any_tools_called

            # Execute each tool call
            any_tools_called = True
            messages.append(response)
            for tool_call in response.tool_calls:
                tool_name = tool_call["name"]
                tool_args = tool_call["args"]
                tool_call_id = tool_call["id"]

                log.info(
                    "agent.tool_call",
                    tool_name=tool_name,
                    tool_args=str(tool_args)[:200],
                )
                tool = tools_by_name.get(tool_name)
                if tool is None:
                    tool_result = f"Error: unknown tool '{tool_name}'"
                    log.warning("agent.unknown_tool", tool_name=tool_name)
                else:
                    try:
                        tool_result = await tool.ainvoke(tool_args)
                    except Exception as exc:
                        tool_result = f"Error executing {tool_name}: {exc}"
                        log.error(
                            "agent.tool_error",
                            tool_name=tool_name,
                            error=str(exc),
                        )

                messages.append(
                    ToolMessage(content=str(tool_result), tool_call_id=tool_call_id)
                )

        # Hit max iterations — return whatever we have
        log.warning(
            "agent.max_iterations",
            iterations=self._max_iterations,
        )
        return (
            "I ran into a limit processing your request. Please try again or simplify.",
            any_tools_called,
        )

    async def _build_system_prompt(self, discord_user_id: str) -> str:
        """Build the system prompt with the user's current preferences."""
        wishlist_json = await self._prefs_repo.get(discord_user_id, "wishlist")
        wishlist = json.loads(wishlist_json) if wishlist_json else None

        location_json = await self._prefs_repo.get(discord_user_id, "location")
        location = json.loads(location_json) if location_json else None

        priorities_json = await self._prefs_repo.get(discord_user_id, "search_priorities")
        priorities = json.loads(priorities_json) if priorities_json else None

        return build_system_prompt(
            wishlist=wishlist,
            location=location,
            search_priorities=priorities,
        )

    async def _load_history(self, discord_user_id: str) -> list[BaseMessage]:
        """Load recent conversation history as LangChain messages.

        Skips messages from tool-call turns (marked with tool_call_id='__TOOL_TURN__')
        because replaying them as plain AIMessages causes the model to mimic
        text-only responses instead of calling tools.
        """
        raw_messages = await self._conversation_repo.load_recent(discord_user_id, limit=20)

        lc_messages: list[BaseMessage] = []
        for msg in raw_messages:
            # Skip tool-call turns — they pollute the model's behavior
            if msg.get("tool_call_id") == "__TOOL_TURN__":
                continue

            role = msg["role"]
            content = msg["content"]
            if role == "user":
                lc_messages.append(HumanMessage(content=content))
            elif role == "assistant":
                lc_messages.append(AIMessage(content=content))
            # Skip tool messages in history replay — they're context-dependent

        return lc_messages


def _extract_text(content: str | list) -> str:
    """Extract plain text from LLM response content.

    Gemini 2.5 Flash returns content as a list of blocks like:
        [{"type": "text", "text": "...", "extras": {...}}]
    while most other models return a plain string.
    """
    if isinstance(content, str):
        return content or "I'm not sure how to respond to that."
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                parts.append(block.get("text", ""))
            elif isinstance(block, str):
                parts.append(block)
        return "".join(parts) or "I'm not sure how to respond to that."
    return str(content) or "I'm not sure how to respond to that."
