"""Agent runner: tool-calling loop with conversation memory."""

from __future__ import annotations

import json
import re
import time
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

from poob.agent.prompts import build_system_prompt
from poob.agent.tools import build_tools
from poob.skills.llm_call import _model_id
from poob.storage.repositories.conversation_repo import ConversationRepository
from poob.storage.repositories.preferences_repo import UserPreferencesRepository
from poob.storage.repositories.watchlist_repo import WatchlistRepository

log = structlog.get_logger()

# Intent-to-tool mapping for unambiguous action requests.
# When the user's message clearly matches one of these intents, we force
# the tool call directly instead of relying on the LLM to select it.
# This prevents the agent brain from misinterpreting "start scan" as
# "show me recent deals" (a known Groq/Llama instruction-following gap).
_FORCED_INTENTS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(?:start|run|trigger|do)\s+(?:a\s+)?(?:scan|patrol|sweep)\b", re.I), "trigger_scan"),
    (re.compile(r"\b(?:scan|patrol|sweep)\s+(?:now|marketplace|please|again|asap)\b", re.I), "trigger_scan"),
    (re.compile(r"^(?:scan|patrol|sweep)$", re.I), "trigger_scan"),
    (re.compile(r"\bpause\s+(?:the\s+)?(?:scan|patrol|sweep)", re.I), "pause_patrol"),
    (re.compile(r"\bresume\s+(?:the\s+)?(?:scan|patrol|sweep)", re.I), "resume_patrol"),
    # Wishlist display — LLMs frequently misroute or drop the data
    (re.compile(r"\b(?:show|list|display|view|what(?:'?s| is| are))\s+(?:my\s+)?(?:wish\s*list|watch\s*list|interests?)\b", re.I), "show_wishlist"),
    (re.compile(r"^(?:wish\s*list|watch\s*list)$", re.I), "show_wishlist"),
]

# Tools whose output contains structured data that MUST reach the user verbatim.
# For these tools, the runner uses a split-channel architecture:
#   - The tool's output IS the primary response (data channel).
#   - The LLM generates only a brief comment (commentary channel).
#   - The runner composes them deterministically: data + commentary.
# This guarantees data visibility without relying on the LLM to echo it.
_DATA_DISPLAY_TOOLS: frozenset[str] = frozenset({
    "show_wishlist",
    "get_recent_deals",
    "get_deal_details",
    "search_listings",
    "get_preferences",
    "get_scan_history",
})

# Injected after a data-display tool result to instruct the LLM to produce
# ONLY a brief comment — the data itself will be shown separately.
_COMMENTARY_ONLY_INSTRUCTION = (
    "[SYSTEM] The above data has ALREADY been shown to the user. "
    "Do NOT repeat, summarize, or list the data. "
    "Write ONLY a brief 1-sentence friendly comment or follow-up question. "
    "Example: 'Let me know if you want to add anything!' or 'Want me to search for any of these?'"
)


def _detect_forced_intent(message: str) -> str | None:
    """Match user message against unambiguous action patterns.

    Returns the tool name to force, or None if the LLM should decide.
    """
    text = message.strip()
    for pattern, tool_name in _FORCED_INTENTS:
        if pattern.search(text):
            return tool_name
    return None


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
        exclusion_repo: Any = None,
        personality: bool = True,
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
        self._exclusion_repo = exclusion_repo
        self._personality = personality

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
            exclusion_repo=self._exclusion_repo,
        )
        tools_by_name = {t.name: t for t in tools}

        # 4. Prepare messages
        messages: list[BaseMessage] = [SystemMessage(content=system_prompt)]
        messages.extend(history)
        messages.append(HumanMessage(content=user_message))

        # 5. Bind tools to LLM
        bound_llm = self._llm.bind_tools(tools)

        # 5b. Force tool call for unambiguous action requests.
        # The LLM sometimes misinterprets "start scan" as "show deals" —
        # this ensures deterministic tool selection for clear commands.
        # After forcing, we run the LLM WITHOUT tools so it can only
        # compose a text response (no additional tool calls like get_recent_deals).
        forced_tool = _detect_forced_intent(user_message)
        if forced_tool and forced_tool in tools_by_name:
            log.info("agent.forced_intent", tool=forced_tool, message=user_message[:60])
            try:
                tool_result = await tools_by_name[forced_tool].ainvoke({})
            except Exception as exc:
                tool_result = f"Error: {exc}"
            tool_result_str = str(tool_result)

            # Inject the tool result and let the LLM compose a text-only response
            messages.append(AIMessage(
                content="",
                tool_calls=[{"name": forced_tool, "args": {}, "id": "forced_0"}],
            ))
            messages.append(ToolMessage(content=tool_result_str, tool_call_id="forced_0"))

            # Split-channel: data-display tools use deterministic composition.
            # The tool output IS the response; the LLM only adds commentary.
            if forced_tool in _DATA_DISPLAY_TOOLS and tool_result_str.strip():
                messages.append(HumanMessage(content=_COMMENTARY_ONLY_INSTRUCTION))
                commentary: AIMessage = await self._llm.ainvoke(messages)
                commentary_text = _extract_text(commentary.content)
                response_text = _compose_data_response(tool_result_str, commentary_text)
            else:
                # Non-data tools: LLM responds normally with NO tools bound
                response: AIMessage = await self._llm.ainvoke(messages)
                response_text = _extract_text(response.content)

            tools_were_called = True
        else:
            # 6. Normal tool-calling loop
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
        last_tool_result = ""
        last_data_tool_result = ""  # Authoritative output from last data-display tool

        model = _model_id(bound_llm)
        for iteration in range(self._max_iterations):
            t0 = time.monotonic()
            response: AIMessage = await bound_llm.ainvoke(messages)
            elapsed = time.monotonic() - t0

            log.info(
                "agent.llm_response",
                iteration=iteration,
                model=model,
                elapsed_s=round(elapsed, 1),
                has_tool_calls=bool(response.tool_calls),
                tool_calls=[tc.get("name", "?") for tc in (response.tool_calls or [])],
                content_type=type(response.content).__name__,
                content_preview=str(response.content)[:120],
            )

            # If no tool calls, we have our final answer
            if not response.tool_calls:
                text = _extract_text(response.content)
                # If LLM returned empty after tool calls, use the last tool result
                if not text.strip() or text == _EMPTY_FALLBACK:
                    if last_tool_result:
                        return last_tool_result, any_tools_called

                # Split-channel: if a data-display tool ran, compose
                # deterministically instead of trusting the LLM's text.
                if last_data_tool_result:
                    # The LLM's text is treated as commentary.
                    # Request commentary-only on next iteration would be ideal,
                    # but since we're already at the final answer, compose now.
                    text = _compose_data_response(last_data_tool_result, text)
                return text, any_tools_called

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

                last_tool_result = str(tool_result)
                if tool_name in _DATA_DISPLAY_TOOLS:
                    last_data_tool_result = last_tool_result
                messages.append(
                    ToolMessage(content=last_tool_result, tool_call_id=tool_call_id)
                )

            # After executing data-display tools, inject the commentary-only
            # instruction so the LLM knows NOT to repeat the data on the next
            # iteration (where it will produce the final text response).
            if last_data_tool_result:
                messages.append(HumanMessage(content=_COMMENTARY_ONLY_INSTRUCTION))

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
            personality=self._personality,
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


_EMPTY_FALLBACK = "I'm not sure how to respond to that."


def _compose_data_response(tool_data: str, llm_commentary: str) -> str:
    """Deterministically compose a data-display response.

    Split-channel architecture: the tool's output is the authoritative data
    and always appears first. The LLM's text is treated as optional commentary
    appended after the data. No heuristics, no dedup — just concatenation.

    Args:
        tool_data: Authoritative output from a data-display tool.
        llm_commentary: LLM-generated commentary (may be empty or redundant).

    Returns:
        Combined response: data first, then commentary.
    """
    data = tool_data.strip()
    commentary = llm_commentary.strip()

    if not data:
        return commentary or _EMPTY_FALLBACK

    # If the LLM produced no meaningful commentary, return data alone.
    if not commentary or commentary == _EMPTY_FALLBACK:
        return data

    return f"{data}\n\n{commentary}"


def _extract_text(content: str | list) -> str:
    """Extract plain text from LLM response content.

    Gemini 2.5 Flash returns content as a list of blocks like:
        [{"type": "text", "text": "...", "extras": {...}}]
    while most other models return a plain string.
    """
    if isinstance(content, str):
        return content or _EMPTY_FALLBACK
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                parts.append(block.get("text", ""))
            elif isinstance(block, str):
                parts.append(block)
        return "".join(parts) or _EMPTY_FALLBACK
    return str(content) or _EMPTY_FALLBACK
