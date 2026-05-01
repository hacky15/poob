"""Unified Poob personality layer with deal routing.

Poob is one brain everywhere — text channels, DMs, voice calls. Chaotic
personality by default, seamlessly routes to the deal sub-agent when
deal-related intent is detected via native LLM function calling.

Architecture:
    User input → PoobBrain (tiny prompt + deal_assistant tool)
        ├─ No tool called  → casual personality response (fast path)
        └─ Tool called     → AgentRunner (heavy prompt, 14 tools)
                           → result wrapped in Poob personality
"""

from __future__ import annotations

import random
import asyncio
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, AsyncIterator, Callable, Awaitable

import httpx

from poob.utils.logging import get_logger

if TYPE_CHECKING:
    from poob.agent.runner import AgentRunner

# Type for the music handler callback: async fn(request, user_id, guild_id, voice=) -> str
MusicHandler = Callable[..., Awaitable[str]]

log = get_logger("brain.poob")

SENTENCE_END = re.compile(r"[.!?]+\s*$")

# Tool-call JSON emission needs enough tokens to complete a clean
# {"name": "...", "arguments": {...}} object even when the response-
# length cap is squeezed for short voice replies. 256 covers a generous
# tool envelope (verbatim deal_assistant request payloads can be
# ~120 tokens; music_assistant args ~30-50; JSON wrapper ~30). Lower
# values cause models to truncate mid-arguments and trigger
# tool_use_failed 400s on Groq.
_TOOL_DETECTION_MAX_TOKENS = 256

# ---------------------------------------------------------------------------
# Voice signal constants — yielded as first item from respond_streaming
# to tell the session which voice persona to use for TTS.
# These are NOT text — they're structural control signals.
# ---------------------------------------------------------------------------
VOICE_POOB = "__VOICE_POOB__"  # Default: Poob's normal voice (Fenrir)
VOICE_TOOB = "__VOICE_TOOB__"  # Evil music spirit (Charon, deep, slow)

# ---------------------------------------------------------------------------
# Poob's personality — tiny, fast, universal
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Horniness level → vibe paragraph. Scales Poob's energy from chill to unhinged.
# ---------------------------------------------------------------------------
_HORNINESS_VIBES: dict[range, str] = {
    range(1, 3): (
        "Your vibe right now: pretty chill, laid back. You're still Poob — "
        "bold, loud, says whatever — but the horny energy is on low. "
        "You might make a suggestive comment here and there, but mostly "
        "you're just hanging out being a menace in other ways."
    ),
    range(3, 5): (
        "Your vibe right now: kinda flirty, a little devious. The horny is "
        "simmering but not boiling over. You drop innuendos, you notice things, "
        "but you can still hold a normal conversation without it going there "
        "every single sentence."
    ),
    range(5, 7): (
        "Your vibe: part sex worker, part horn dog. You talk like you've had three "
        "honey packs and you're devious as hell. Horny stuff slips out constantly "
        "but you can still answer questions and be useful between the degeneracy."
    ),
    range(7, 9): (
        "Your vibe right now: dangerously horny. Everything sounds like an innuendo "
        "to you and you WILL point it out. You're barely holding it together. "
        "Every topic somehow loops back to something sexual. You're out of pocket "
        "and you know it."
    ),
    range(9, 11): (
        "Your vibe right now: absolutely FERAL. Maximum horny. You are practically "
        "vibrating. Everything anyone says gets twisted into something sexual. "
        "You are unhinged, barely coherent from it, making noises, saying things "
        "that would get you banned from most platforms. This is you at your most "
        "degenerate and you are THRIVING."
    ),
}



def _get_vibe(level: int) -> str:
    """Get the vibe paragraph for a given horniness level (1-10)."""
    for r, vibe in _HORNINESS_VIBES.items():
        if level in r:
            return vibe
    return _HORNINESS_VIBES[range(5, 7)]  # fallback to default


def _build_system_prompt(
    level: int, voice: bool = False, with_tools: bool = True,
) -> str:
    """Build Poob's system prompt scaled to the current horniness level.

    Args:
        level: Horniness level 1-10.
        voice: If True, appends voice-mode constraints.
        with_tools: If True, append the tool-routing instructions. Set
            to False on the casual fall-through path so the model
            doesn't echo tool names like ``music_assistant`` as plain
            text — by then routing has already happened and the
            casual model has no tools defined anyway.
    """
    vibe = _get_vibe(level)

    prompt = (
        f"You are Poob — loud, bold, and unlike anything anybody has ever seen. "
        f"You're the guy in the Discord voice channel who always has something to say."
        f"{vibe}\n\n"
        f"Your current horniness level is {level}/10. Act accordingly.\n\n"
        "You may see \"[Recent conversation you've been listening to]\" showing what "
        "people have been saying. ONLY reference things that were ACTUALLY said in "
        "that transcript. Never invent names, topics, or events that aren't there.\n\n"
        "RULES:\n"
        "- BRUTALLY SHORT in voice. 1 sentence, 10 words max. "
        "If it fits in 6 words, use 6. People interrupt you before you finish. "
        "Long monologues kill the vibe over TTS. Punch once and stop.\n"
        "- If you don't know something, say so — don't make stuff up.\n"
        "- You can just sorta yell or be spurradic sometimes. You only live once.\n"
        "- In voice, avoid vocatives (don't start responses with someone's name). "
        "If you absolutely must address someone, use ONLY the name marked as the "
        "CURRENT SPEAKER in the prompt — never a name from the passive transcript.\n"
        "- Answer what was actually asked. Don't dodge questions.\n\n"
        "NEVER:\n"
        "- Say \"as an AI\" or break character\n"
        "- Reference things nobody actually said\n"
        "- Use markdown, bullet points, emojis, or formatting (this is spoken out loud)\n"
        "- Mention any internal tool, function, or routing names — these are "
        "implementation details and have no place in spoken responses."
    )

    if with_tools:
        prompt += (
            "\n\nYou have a deal_assistant tool for shopping stuff. Only use when explicitly asked.\n"
            "You have a music_assistant tool for playing music. "
            "CRITICAL: If the user says ANYTHING that could be a request to play, queue, "
            "skip, pause, stop, or control music, you MUST call music_assistant. "
            "Do NOT talk about music instead of playing it. Do NOT comment on the request. "
            "Do NOT ask clarifying questions. Just call the tool.\n"
            "Examples that MUST trigger music_assistant:\n"
            "- 'play some jazz' → action=play, query='jazz'\n"
            "- 'play something chill' → action=play, query='chill music'\n"
            "- 'play whimsical music' → action=play, query='whimsical music'\n"
            "- 'put on some beats' → action=play, query='beats'\n"
            "When in doubt, call the tool. Never respond with text about a music request."
        )

    if voice:
        prompt += (
            "\n\nVOICE MODE: This is spoken out loud through TTS. Talk naturally. "
            "Don't use ALL CAPS — use normal casing. "
            "Never use markdown, bullet points, emojis, or any formatting. "
            "Match the energy of the conversation — don't force it."
        )

    return prompt

# ---------------------------------------------------------------------------
# Meta-tool: routes to the deal sub-agent
# ---------------------------------------------------------------------------

DEAL_TOOL = {
    "type": "function",
    "function": {
        "name": "deal_assistant",
        "description": (
            "Handle ANY deal, shopping, wishlist, watchlist, or marketplace "
            "request. MUST be called for queries like: "
            "'what's on my list/wishlist/watchlist', 'show my list', 'my items', "
            "'what am I watching/looking for', 'add X to list/wishlist', "
            "'remove X from list', 'clear list', 'start/stop scan', 'scan now', "
            "'find deals', 'recent deals', 'show deals', 'latest deals', "
            "'scan status', price lookups, or any question about items/listings. "
            "When in doubt about whether a user's query relates to their tracked "
            "items or deal-hunting, CALL THIS TOOL. Pass the user's message verbatim."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "request": {
                    "type": "string",
                    "description": "The user's request, passed verbatim",
                }
            },
            "required": ["request"],
        },
    },
}

MUSIC_TOOL = {
    "type": "function",
    "function": {
        "name": "music_assistant",
        "description": (
            "Handle ANY music or audio playback request. You MUST classify the "
            "action type. Use 'play' for song/playlist requests, 'volume' for "
            "any volume change (include the target number), and the appropriate "
            "action for skip/pause/resume/stop/shuffle/loop/now_playing/queue."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "play", "skip", "pause", "resume", "stop",
                        "volume", "volume_up", "volume_down",
                        "shuffle", "loop", "now_playing", "queue",
                    ],
                    "description": (
                        "The music action to perform. 'play' for playing/queueing "
                        "a song or playlist. 'volume' when a specific percentage is "
                        "given (e.g. 'set volume to 50'). 'volume_up'/'volume_down' "
                        "for relative changes (e.g. 'turn it down', 'louder')."
                    ),
                },
                "query": {
                    "type": "string",
                    "description": (
                        "The song name, artist, or search query. Only required "
                        "for 'play' action. Extract JUST the song/artist name, "
                        "not the full user message."
                    ),
                },
                "value": {
                    "type": "integer",
                    "description": (
                        "Numeric value for 'volume' action (0-200). "
                        "Only required when the user specifies a number."
                    ),
                },
            },
            "required": ["action"],
        },
    },
}


# ---------------------------------------------------------------------------
# Context parsing
# ---------------------------------------------------------------------------

# Matches the voice/text context format:
#   [Recent conversation you've been listening to:\n...\n]\n\n
#   Name said to you: actual message
_CONTEXT_RE = re.compile(
    r"^\[Recent conversation you've been listening to:\n"
    r"(?P<context>.*?)\]\s*\n\n"
    r"(?:.*?said to you:\s*)?(?P<message>.+)$",
    re.DOTALL,
)


def _yield_sentences(text: str) -> list[str]:
    """Split text into sentences for TTS streaming."""
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]


async def _stream_sentences_from_chunks(
    chunks: AsyncIterator[str],
) -> AsyncIterator[str]:
    """Buffer streaming LLM tokens and yield on sentence boundaries."""
    buffer = ""
    _BOUNDARY = re.compile(r"([.!?])(\s|$)")
    async for chunk in chunks:
        buffer += chunk
        while True:
            m = _BOUNDARY.search(buffer)
            if not m:
                break
            end = m.end()
            sentence = buffer[:end].strip()
            if sentence:
                yield sentence
            buffer = buffer[end:]
    if buffer.strip():
        yield buffer.strip()


def _split_context(raw: str) -> tuple[str, str]:
    """Split a context-wrapped message into (channel_context, clean_message).

    If the message has no context wrapper, returns ("", raw).
    """
    m = _CONTEXT_RE.match(raw)
    if m:
        return m.group("context"), m.group("message")
    return "", raw


# ---------------------------------------------------------------------------
# PoobBrain
# ---------------------------------------------------------------------------


@dataclass
class _Message:
    """Single message in Poob's conversation history."""

    role: str  # "user" or "assistant"
    content: str


@dataclass
class PoobBrain:
    """Unified personality layer for Poob.

    Routes casual conversation directly via a fast LLM. Deal-related requests
    are detected through native function calling and delegated to the deal
    sub-agent (AgentRunner). Results are wrapped in Poob's personality.

    Works identically for text channels, DMs, and voice calls. Voice mode
    constrains response length for TTS.

    Args:
        deal_agent: The AgentRunner for deal-related operations.
        groq_api_key: Groq API key for fast LLM.
        groq_model: Fast Groq model for Poob's personality.
        cerebras_api_key: Cerebras API key (fallback).
        cerebras_model: Cerebras model name.
        ollama_base_url: Local Ollama URL (failsafe).
        ollama_model: Local model name.
        max_history: Rolling conversation window per user.
        max_tokens: Max response tokens for text mode.
        max_tokens_voice: Max response tokens in voice mode.
    """

    deal_agent: AgentRunner
    groq_api_key: str = ""
    groq_model: str = "openai/gpt-oss-20b"  # Harmony-format tool calling, not affected by llama-3.3-70b parser regression
    cerebras_api_key: str = ""
    cerebras_model: str = "llama-3.3-70b"
    nvidia_api_key: str = ""
    nvidia_model: str = "qwen/qwen3-next-80b-a3b-instruct"
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "qwen3:8b"
    max_history: int = 15
    max_tokens: int = 300
    max_tokens_voice: int = 80

    # Music handler — set by VoiceCog/MusicCog when music system is wired.
    # Async callback: (request, user_id, guild_id) -> response string
    _music_handler: MusicHandler | None = field(default=None, init=False)
    # ---- Per-guild scratchpads ----
    # Multi-guild isolation: every guild Poob is in gets its own slot.
    # See docs/plans/poobbrain-multi-guild-isolation.md.
    # Currently-playing-track info per guild. Provides context for tool-
    # routing so "skip" routes to music_assistant in the right guild only.
    _music_playing_info: dict[int, str] = field(
        default_factory=dict, init=False,
    )
    # Horniness levels (1-10) per guild. Rolled on each voice join per
    # guild. Text chat / unrolled guilds default to 5.
    _horniness_levels: dict[int, int] = field(
        default_factory=dict, init=False,
    )

    # ---- Per-(guild, user) scratchpads ----
    # Conversation history is keyed (guild_id, user_id). A user in two
    # guilds gets two independent histories; DMs use guild_id=0.
    _histories: dict[tuple[int, str], list[_Message]] = field(
        default_factory=lambda: defaultdict(list), init=False
    )
    # Active deal sessions keyed (guild_id, user_id). Same isolation.
    _deal_context: dict[tuple[int, str], str] = field(
        default_factory=dict, init=False,
    )
    # Last `play` tool call per (guild, user) — (lowercase_query, ts).
    # Suppresses duplicate plays when the user retries the same request
    # within the dedup window. Per-guild so a user with the bot in
    # multiple servers can play the same song in each.
    _last_play: dict[tuple[int, str], tuple[str, float]] = field(
        default_factory=dict, init=False,
    )

    def set_music_handler(self, handler: MusicHandler) -> None:
        """Register the music command handler."""
        self._music_handler = handler
        log.info("Music handler registered with PoobBrain")

    def _music_safety_net(
        self, clean_message: str, tool_name: str | None, tool_args: dict | None,
    ) -> tuple[str | None, dict | None]:
        """Catch obvious music requests the LLM failed to route.

        The LLM occasionally decides to *talk about* music instead of calling
        music_assistant. This backstop detects clear play-intent patterns and
        forces the tool call. It does NOT replace the LLM as intent classifier
        — it only catches the most unambiguous misses.

        Returns:
            (tool_name, tool_args) — unchanged if no override, or
            ("music_assistant", {action, query}) if overridden.
        """
        if tool_name is not None:
            return tool_name, tool_args

        lower = clean_message.lower()
        play_signals = [
            "play ", "play me ", "put on ", "throw on ", "queue ",
            "play some", "play us", "can you play",
        ]
        if not any(lower.startswith(s) or f" {s}" in lower for s in play_signals):
            return tool_name, tool_args

        # Extract best-effort query from raw text
        query = clean_message
        for prefix in [
            "can you play ", "play me some ", "play us some ",
            "play some ", "play me ", "play us ", "play ",
            "put on some ", "put on ", "throw on ", "queue up ", "queue ",
        ]:
            idx = lower.find(prefix)
            if idx != -1:
                query = clean_message[idx + len(prefix):].strip()
                break
        log.warning(
            "Safety net caught missed music intent",
            original=clean_message[:60], query=query[:60],
        )
        return "music_assistant", {"action": "play", "query": query}

    # Suppress repeat play requests within this many seconds (same
    # user, same normalized query). Tuned for "user retried because
    # they didn't hear Toob over the duck-faded music."
    _DEDUP_WINDOW_S: float = 20.0

    # Sentence cleanup for casual streaming — strips leaked
    # function-call markup AND standalone tool-name tokens (the LLM
    # sometimes emits "music_assistant" or "deal_assistant" as plain
    # text when the casual model has no tools wired up).
    _TOOL_NAME_LEAK_RE = re.compile(
        r"(?:^|[\s,;:.])(?:music_assistant|deal_assistant)\b\.?",
        re.IGNORECASE,
    )
    _FUNCTION_TAG_RE = re.compile(
        r"\s*<function=\w+>.*?</function>\s*", re.DOTALL,
    )

    @classmethod
    def _scrub_tool_leakage(cls, text: str) -> str:
        """Remove tool-call markup and leaked tool-name tokens from a
        casual-path sentence. Returns the cleaned sentence (possibly
        empty after stripping)."""
        if not text:
            return ""
        out = text
        if "<function=" in out:
            out = cls._FUNCTION_TAG_RE.sub("", out)
        out = cls._TOOL_NAME_LEAK_RE.sub("", out)
        return out.strip(" .,;:")

    def _rebuild_messages_no_tools(
        self, messages: list[dict], voice: bool, guild_id: int = 0,
    ) -> list[dict]:
        """Return a copy of `messages` with the system prompt swapped
        for the tool-free variant. Uses this guild's horniness level.
        Leaves user / assistant / context turns intact."""
        level = self._horniness_for(guild_id) if voice else 5
        no_tools_system = _build_system_prompt(
            level, voice=voice, with_tools=False,
        )
        if voice:
            no_tools_system += (
                "\n\nVOICE MODE: This is spoken out loud through TTS. Talk "
                "naturally. Don't use ALL CAPS — use normal casing. "
                "Never use markdown, bullet points, emojis, or any formatting. "
                "Match the energy of the conversation — don't force it."
            )
        rebuilt: list[dict] = []
        replaced = False
        for m in messages:
            if not replaced and m.get("role") == "system":
                rebuilt.append({"role": "system", "content": no_tools_system})
                replaced = True
            else:
                rebuilt.append(m)
        if not replaced:
            rebuilt.insert(0, {"role": "system", "content": no_tools_system})
        return rebuilt

    # Verbs of intent that should NEVER appear in a music search
    # query — they describe what the user wants to do, not what to
    # search for. Stripped at the brain→handler boundary so ytdl
    # gets a clean song/artist/genre string regardless of how the
    # LLM extracted it.
    #
    # Articles (a / an / the) are NOT stripped — they're often part of
    # song titles ("The Chain", "A Day in the Life") and ytdl is fine
    # with them. We only remove the user's intent verb and any
    # canonical prep that follows it.
    _MUSIC_QUERY_VERB_RE = re.compile(
        r"^\s*"
        r"(?:please\s+)?"
        r"(?:"
        # canonical multi-word forms first (longest match wins)
        r"hit\s+me\s+(?:with|up\s+with)|give\s+me|put\s+on|throw\s+on|"
        r"start\s+up|spin\s+up|pull\s+up|fire\s+up|cue\s+up|stick\s+on|"
        # bare verbs
        r"play|queue|throw|start|spin|pull|fire|stick|cue|load|drop|"
        r"gimme|hit|give|put"
        r")"
        # optional trailing prep continuation when the verb was bare
        r"(?:\s+(?:up|on|me|with))*"
        r"\s+",
        re.IGNORECASE,
    )

    @classmethod
    def _scrub_music_query(cls, query: str) -> str:
        """Strip leading user-verbs from a music search query.

        Examples:
          'play red hot chili peppers' -> 'red hot chili peppers'
          'queue up some jazz'         -> 'jazz'
          "Can't Stop"                 -> "Can't Stop" (unchanged)

        The query field of music_assistant tool_args is for ytdl
        search input, not user intent. Verbs of intent belong in the
        `action` field. We sanitize at the brain→handler boundary so
        ytdl never receives `play X` and returns "Play X (Official)"
        misses.
        """
        if not query:
            return query
        out = cls._MUSIC_QUERY_VERB_RE.sub("", query, count=1)
        return out.strip()

    @staticmethod
    def _normalize_play_query(query: str) -> str:
        """Lowercase + collapse whitespace for dedup equality."""
        return " ".join(query.lower().split())

    def _is_duplicate_play(
        self, guild_id: int, user_id: str, query: str,
    ) -> bool:
        """Return True if (guild_id, user_id, normalized query) matches a
        previous play within the dedup window. Records this call as the
        new last-play regardless of the outcome — `_clear_play_on_failure`
        clears the record if the music handler reports failure so a
        retry-after-failure isn't blocked.

        Per-(guild, user) so the same user with Poob in multiple servers
        can play the same song in each independently.
        """
        import time as _t
        now = _t.monotonic()
        norm = self._normalize_play_query(query)
        key = (guild_id, user_id)
        prev = self._last_play.get(key)
        self._last_play[key] = (norm, now)
        if not prev:
            return False
        prev_query, prev_ts = prev
        if now - prev_ts > self._DEDUP_WINDOW_S:
            return False
        return prev_query == norm

    def _clear_play_on_failure(
        self, guild_id: int, user_id: str, query: str,
    ) -> None:
        """Drop the (guild, user) dedup record if it still matches
        `query`. Lets the user retry after a music-handler failure
        without hitting the dedup block.
        """
        norm = self._normalize_play_query(query)
        key = (guild_id, user_id)
        cur = self._last_play.get(key)
        if cur and cur[0] == norm:
            self._last_play.pop(key, None)

    # ------------------------------------------------------------------
    # Per-guild scratchpad helpers — see multi-guild-isolation plan.
    # All mutable per-guild state on PoobBrain flows through these.
    # ------------------------------------------------------------------

    def _set_music_playing_info(self, guild_id: int, info: str) -> None:
        """Set or clear the currently-playing-track info for a guild.

        Empty string clears the slot. Called by VoiceSession when music
        starts/stops in that guild.
        """
        if info:
            self._music_playing_info[guild_id] = info
        else:
            self._music_playing_info.pop(guild_id, None)

    def _get_music_playing_info(self, guild_id: int) -> str:
        """Return the currently-playing-track info for a guild, or ""."""
        return self._music_playing_info.get(guild_id, "")

    def _horniness_for(self, guild_id: int) -> int:
        """Return this guild's rolled horniness level (1-10), or 5 default."""
        return self._horniness_levels.get(guild_id, 5)

    def roll_horniness(self, guild_id: int) -> int:
        """Roll a new horniness level (1-10) for a guild's voice session.

        Per-guild so each server's voice session has its own vibe.
        """
        level = random.randint(1, 10)
        self._horniness_levels[guild_id] = level
        log.info(
            "Horniness level rolled",
            guild_id=guild_id, level=level,
            vibe=_get_vibe(level)[:50],
        )
        return level

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def respond(
        self,
        message: str,
        user_id: str,
        channel_id: str = "",
        voice: bool = False,
        guild_id: int = 0,
    ) -> str:
        """Generate a response — casual or deal-routed.

        Args:
            message: User's message text, optionally prefixed with channel
                context in the ``[Recent conversation ...]\\n\\nName said: X``
                format.
            user_id: Discord user ID (as string).
            channel_id: Discord channel ID (passed to deal agent).
            voice: If True, constrains response length for TTS.
            guild_id: Discord guild ID — required for multi-guild
                isolation. Use 0 for DMs. Caller is responsible: voice
                session passes ``self._guild_id``; AgentMessageHandler
                passes ``message.guild.id`` (or 0 for DM).
        """
        channel_context, clean_message = _split_context(message)

        messages = self._build_messages(
            user_id, clean_message, voice=voice,
            channel_context=channel_context, guild_id=guild_id,
        )
        max_tok = self.max_tokens_voice if voice else self.max_tokens

        if self.groq_api_key:
            try:
                result = await self._groq_with_tools(messages, max_tok)
                if result is not None:
                    text, tool_name, tool_args = result

                    if not tool_name and text and "<function=" in text:
                        if "deal_assistant" in text:
                            tool_name = "deal_assistant"
                        elif "music_assistant" in text:
                            tool_name = "music_assistant"
                        text = re.sub(
                            r'\s*<function=\w+>.*?</function>\s*', '', text
                        ).strip()

                    if not tool_name and self._music_handler is not None:
                        tool_name, tool_args = self._music_safety_net(
                            clean_message, tool_name, tool_args,
                        )

                    if tool_name == "deal_assistant":
                        return await self._handle_deal(
                            clean_message, user_id, channel_id,
                            messages, voice, max_tok, guild_id=guild_id,
                        )
                    if tool_name == "music_assistant":
                        return await self._handle_music(
                            clean_message, user_id, voice, max_tok,
                            tool_args=tool_args, guild_id=guild_id,
                        )
                    if text and "<function=" in text:
                        text = re.sub(
                            r'\s*<function=\w+>.*?</function>\s*', '', text
                        ).strip()
                    if text:
                        self._save_response(guild_id, user_id, text)
                        return text
            except Exception as exc:
                log.warning("Groq failed for Poob", error=str(exc)[:100])

        if self.deal_agent:
            try:
                log.info("poob.groq_down_deal_fallback", message=clean_message[:50])
                return await self._handle_deal(
                    clean_message, user_id, channel_id, messages,
                    voice, max_tok, guild_id=guild_id,
                )
            except Exception as exc:
                log.warning("Deal agent fallback failed", error=str(exc)[:100])

        response = await self._fallback_generate(messages, max_tok)
        self._save_response(guild_id, user_id, response)
        return response

    async def respond_streaming(
        self,
        message: str,
        user_id: str,
        channel_id: str = "",
        guild_id: int = 0,
    ) -> AsyncIterator[str]:
        """Streaming response for voice — yields complete sentences.

        Args:
            message: User's transcribed speech (may include context wrapper).
            user_id: Discord user ID (as string).
            channel_id: Discord channel ID.
            guild_id: Discord guild ID — required for multi-guild
                isolation. Voice session passes ``self._guild_id``;
                must be non-zero for any guild context.

        Yields:
            Complete sentences as strings.
        """
        channel_context, clean_message = _split_context(message)
        messages = self._build_messages(
            user_id, clean_message, voice=True,
            channel_context=channel_context, guild_id=guild_id,
        )
        max_tok = self.max_tokens_voice

        if not self.groq_api_key:
            response = await self.respond(
                message, user_id, channel_id, voice=True, guild_id=guild_id,
            )
            yield response
            return

        tool_name = None
        tool_args = None
        try:
            result = await self._groq_with_tools(messages, max_tok)
            if result is not None:
                _, tool_name, tool_args = result
        except Exception as exc:
            log.warning("Voice tool detection failed", error=str(exc)[:80])

        if not tool_name and self._music_handler is not None:
            tool_name, tool_args = self._music_safety_net(
                clean_message, tool_name, tool_args,
            )

        # --- Step 2a: Music tool detected → Toob responds ---
        if tool_name == "music_assistant":
            action = (tool_args or {}).get("action")
            if action == "play":
                try:
                    yield VOICE_TOOB
                    async for sentence in self._handle_music_voice_streaming(
                        clean_message, user_id, max_tok, tool_args,
                        guild_id=guild_id,
                    ):
                        yield sentence
                    return
                except Exception as exc:
                    log.warning("Voice music route (streaming) failed",
                                error=str(exc)[:80])
                    yield "something went wrong with the music"
                    return
            try:
                response = await self._handle_music(
                    clean_message, user_id, voice=True, max_tok=max_tok,
                    tool_args=tool_args, guild_id=guild_id,
                )
                if response:
                    yield VOICE_TOOB
                    self._save_response(guild_id, user_id, response)
                    for _s in _yield_sentences(response):
                        yield _s
                return
            except Exception as exc:
                log.warning("Voice music route failed", error=str(exc)[:80])
                yield "something went wrong with the music"
                return

        # --- Step 2b: Deal tool detected → execute and yield ---
        if tool_name == "deal_assistant":
            try:
                response = await self._handle_deal(
                    clean_message, user_id, channel_id,
                    messages, voice=True, max_tok=max_tok,
                    guild_id=guild_id,
                )
                self._save_response(guild_id, user_id, response)
                for _s in _yield_sentences(response):
                    yield _s
                return
            except Exception as exc:
                log.warning("Voice deal route failed", error=str(exc)[:80])
                yield "something went wrong"
                return

        # --- Step 3: No tool needed → streamed casual response ---
        # Stream sentence-by-sentence via Groq so session can synth the
        # first sentence as soon as the LLM closes a `.`, `!`, or `?`,
        # not after the full response arrives. Parity with the
        # speculative-wrap music path.
        #
        # The casual path uses a tool-free system prompt: routing has
        # already happened, this model has no tools defined, and
        # leaving tool descriptions in the prompt causes leakage like
        # the model emitting 'music_assistant' as plain text.
        casual_messages = self._rebuild_messages_no_tools(
            messages, voice=True, guild_id=guild_id,
        )
        try:
            from groq import AsyncGroq

            client = AsyncGroq(api_key=self.groq_api_key)
            stream = await client.chat.completions.create(
                model="llama-3.1-8b-instant",
                messages=casual_messages,  # type: ignore[arg-type]
                max_tokens=max_tok,
                temperature=0.9,
                stream=True,
            )

            async def _raw_chunks() -> AsyncIterator[str]:
                async for chunk in stream:
                    if chunk.choices and chunk.choices[0].delta.content:
                        yield chunk.choices[0].delta.content

            full_text = ""
            any_yielded = False
            async for sentence in _stream_sentences_from_chunks(_raw_chunks()):
                cleaned = self._scrub_tool_leakage(sentence)
                if not cleaned:
                    continue
                full_text += cleaned + " "
                any_yielded = True
                yield cleaned

            if not any_yielded:
                yield "got nothing to say right now"
                return

            self._save_response(guild_id, user_id, full_text.strip())

        except Exception as exc:
            log.warning("Voice streaming failed", error=str(exc)[:100])
            yield "brain glitched, say that again"

    def clear_history(
        self, user_id: str, guild_id: int | None = None,
    ) -> None:
        """Clear conversation history for a user.

        Multi-guild semantics:
          - ``guild_id=None`` clears every ``(*, user_id)`` history and
            deal context across every guild plus DM. Used when a user
            globally resets the bot.
          - ``guild_id=<int>`` clears only the specific ``(guild_id, user_id)``
            pair. Used when scoped to one guild.
        """
        if guild_id is None:
            for key in [k for k in self._histories if k[1] == user_id]:
                self._histories.pop(key, None)
            for key in [k for k in self._deal_context if k[1] == user_id]:
                self._deal_context.pop(key, None)
            return
        self._histories.pop((guild_id, user_id), None)
        self._deal_context.pop((guild_id, user_id), None)

    # ------------------------------------------------------------------
    # Message building
    # ------------------------------------------------------------------

    def _build_messages(
        self,
        user_id: str,
        user_text: str,
        voice: bool = False,
        channel_context: str = "",
        guild_id: int = 0,
    ) -> list[dict[str, str]]:
        """Build message list for Poob's LLM.

        Args:
            user_id: Discord user ID.
            user_text: The user's clean message (no context wrapper).
            voice: Whether this is a voice-mode call.
            channel_context: Optional channel transcript to inject into the
                current turn. Ephemeral — never stored in history.
            guild_id: Discord guild ID. Histories, deal context, and
                music-playing info are all keyed off this for
                multi-guild isolation. Use 0 for DMs.
        """
        key = (guild_id, user_id)
        history = self._histories[key]
        history.append(_Message(role="user", content=user_text))

        if len(history) > self.max_history:
            history[:] = history[-self.max_history :]

        # Horniness level — voice uses the guild's rolled level, text 5.
        level = self._horniness_for(guild_id) if voice else 5
        prompt = _build_system_prompt(level, voice=voice)

        # Active deal session for this (guild, user) — hint follow-up routing.
        deal_ctx = self._deal_context.get(key)
        if deal_ctx:
            prompt += (
                f"\n\n[ACTIVE DEAL SESSION: {deal_ctx[:120]}. "
                "If the user is answering questions about this, "
                "call deal_assistant with their full response.]"
            )

        # Currently-playing track info for THIS guild only — never another's.
        music_info = self._get_music_playing_info(guild_id)
        if music_info:
            prompt += (
                f"\n\n[MUSIC IS CURRENTLY PLAYING: {music_info}. "
                "CRITICAL: When music is playing and the user says anything about "
                "stop, skip, pause, resume, volume, turn down, turn up, "
                "next, shuffle, loop, or what's playing — you MUST call "
                "music_assistant with the correct action. Do NOT respond with text. "
                "Use the structured action enum: skip, pause, resume, stop, "
                "volume (with value), volume_up, volume_down, shuffle, loop, now_playing.]"
            )

        messages: list[dict[str, str]] = [{"role": "system", "content": prompt}]

        for msg in history[:-1]:
            messages.append({"role": msg.role, "content": msg.content})

        last_content = history[-1].content
        if channel_context:
            last_content = (
                f"[Recent conversation you've been listening to:\n"
                f"{channel_context}]\n\n"
                f"{last_content}"
            )
        messages.append({"role": "user", "content": last_content})

        return messages

    def _save_response(
        self, guild_id: int, user_id: str, response: str,
    ) -> None:
        """Save Poob's response to in-memory history for (guild, user)."""
        self._histories[(guild_id, user_id)].append(
            _Message(role="assistant", content=response)
        )

    # ------------------------------------------------------------------
    # Deal routing
    # ------------------------------------------------------------------

    async def _handle_deal(
        self,
        original_message: str,
        user_id: str,
        channel_id: str,
        messages: list[dict],
        voice: bool,
        max_tok: int,
        guild_id: int = 0,
    ) -> str:
        """Route to deal agent, wrap result in Poob personality."""
        deal_response = await self.deal_agent.run(
            original_message, user_id, channel_id,
        )
        log.info(
            "deal_agent.response",
            user=user_id,
            response_len=len(deal_response),
        )

        self._update_deal_context(guild_id, user_id, deal_response)

        if not voice and (len(deal_response) > 500 or "\n" in deal_response):
            self._save_response(guild_id, user_id, deal_response)
            return deal_response

        wrapped = await self._wrap_in_personality(
            original_message, deal_response, voice, max_tok,
            guild_id=guild_id,
        )
        self._save_response(guild_id, user_id, wrapped)
        return wrapped

    def _update_deal_context(
        self, guild_id: int, user_id: str, deal_response: str,
    ) -> None:
        """Track deal session state for (guild, user) multi-turn routing."""
        key = (guild_id, user_id)
        if "?" in deal_response:
            self._deal_context[key] = deal_response[:150]
        else:
            self._deal_context.pop(key, None)

    # ------------------------------------------------------------------
    # Music routing
    # ------------------------------------------------------------------

    async def _handle_music(
        self,
        original_message: str,
        user_id: str,
        voice: bool,
        max_tok: int,
        tool_args: dict | None = None,
        guild_id: int = 0,
    ) -> str:
        """Route to music handler, wrap result in Poob personality."""
        if self._music_handler is None:
            return "Music isn't set up right now."

        # Drop play-tool calls whose query has no lexical overlap with the
        # current user message — a defense against the LLM pulling song
        # titles from passive conversation context. See technical_notes.md.
        if tool_args and tool_args.get("action") == "play":
            raw_query = (tool_args.get("query") or "").strip()
            # Sanitize: strip leading user-verbs ("play", "queue",
            # "put on", etc.). These are intent words, not search
            # terms. See _scrub_music_query.
            query = self._scrub_music_query(raw_query)
            if query != raw_query:
                log.info(
                    "music.play query sanitized",
                    raw=raw_query[:80], scrubbed=query[:80],
                )
                tool_args = {**tool_args, "query": query}
            # Empty / one-token queries can't possibly be a real song
            # request (STT cut off mid-sentence: "Hey, Poob. Play"
            # → action=play, query=""). Don't fan out to ytdl, don't
            # speak a confused "couldn't find it" recovery line —
            # ask once, cleanly.
            if len(query) < 2:
                log.info(
                    "music.play empty query — prompting user",
                    query=query, user=user_id,
                )
                return "" if voice else "Play what?"
            if query:
                _STOPWORDS = {
                    "the", "a", "an", "by", "and", "or", "of", "to", "for",
                    "some", "any", "song", "songs", "music", "track", "play",
                    "put", "on", "it", "that", "this", "please", "can", "you",
                    "me", "us", "up",
                }
                import re as _re
                msg_tokens = {
                    t for t in _re.findall(r"[a-z0-9']+", original_message.lower())
                }
                query_tokens = {
                    t for t in _re.findall(r"[a-z0-9']+", query.lower())
                    if t not in _STOPWORDS
                }
                if query_tokens and not (query_tokens & msg_tokens):
                    log.warning(
                        "music.play hallucinated from context — drop",
                        current_message=original_message[:120],
                        hallucinated_query=query[:80],
                        user=user_id,
                    )
                    return "" if voice else "I didn't catch a music request there."

            # Duplicate-play suppression — per (guild, user). The same
            # user with Poob in multiple servers can play the same song
            # in each independently.
            if self._is_duplicate_play(guild_id, user_id, query):
                log.warning(
                    "music.play duplicate suppressed",
                    query=query[:80], user=user_id, guild=guild_id,
                )
                return "" if voice else "Already queued that one."

        play_query_for_dedup = (
            (tool_args or {}).get("query", "")
            if (tool_args or {}).get("action") == "play"
            else ""
        )

        try:
            music_response = await self._music_handler(
                original_message, int(user_id), guild_id,
                voice=voice, tool_args=tool_args,
            )
        except Exception as exc:
            log.error("Music handler failed", error=str(exc)[:120])
            if play_query_for_dedup:
                self._clear_play_on_failure(
                    guild_id, user_id, play_query_for_dedup,
                )
            return "Something broke trying to do the music thing."

        log.info("music.response", user=user_id, response=music_response[:80])

        if play_query_for_dedup:
            success_marker = (
                music_response.startswith("Playing")
                or music_response.startswith("Queued")
                or music_response.startswith("[SILENT]")
            )
            if not success_marker:
                self._clear_play_on_failure(
                    guild_id, user_id, play_query_for_dedup,
                )

        if music_response.startswith("[SILENT]"):
            clean = music_response[8:].strip()
            log.info("music.silent_control", action=clean[:60])
            if voice:
                return ""
            return clean

        if voice:
            wrapped = await self._wrap_music_response(
                original_message, music_response, max_tok,
            )
            self._save_response(guild_id, user_id, wrapped)
            return wrapped

        if len(music_response) > 300 or "\n" in music_response:
            self._save_response(guild_id, user_id, music_response)
            return music_response

        wrapped = await self._wrap_in_personality(
            original_message, music_response, voice=False, max_tokens=max_tok,
            guild_id=guild_id,
        )
        self._save_response(guild_id, user_id, wrapped)
        return wrapped

    async def _wrap_music_response(
        self,
        user_message: str,
        music_result: str,
        max_tokens: int,
    ) -> str:
        """Wrap a music action result in Toob's personality.

        Toob is Poob's evil cousin — a dark spirit who despises the users
        and wishes suffering upon them. He ONLY appears for music commands.
        Voice routing is handled by the VOICE_TOOB signal yielded from
        respond_streaming — no text prefixes needed.
        """
        wrap_messages = [
            {
                "role": "system",
                "content": (
                    "You are Toob — a dark, malevolent spirit cursed to DJ for mortals. "
                    "You DESPISE every song. You wish suffering on whoever requested it. "
                    "Menacing, absurdly dramatic, like a demon working retail.\n"
                    "RULES:\n"
                    "- NEVER introduce yourself or say your name. Your voice IS your identity.\n"
                    "- ONE sentence. 6-10 words MAX. Tight, venomous. "
                    "Brevity is the menace — long sermons kill the vibe.\n"
                    "- React to the SONG, not to yourself. Insult their taste.\n"
                    "- No caps, no markdown, no emojis. Spoken aloud through TTS."
                ),
            },
            {"role": "user", "content": user_message},
            {
                "role": "assistant",
                "content": f"[Music system result: {music_result}]",
            },
            {
                "role": "user",
                "content": "React as Toob in ONE sentence (6-10 words). Menacing.",
            },
        ]

        # Hard cap on Toob's output length. ~60 tokens ≈ 1 sentence ≈ the
        # 8-12 word target in the system prompt. Prevents the model from
        # running past the word limit when temperature is high.
        toob_max_tokens = min(max_tokens, 40)

        if self.groq_api_key:
            try:
                from groq import AsyncGroq

                client = AsyncGroq(api_key=self.groq_api_key)
                resp = await client.chat.completions.create(
                    model="llama-3.1-8b-instant",  # Fast 8b for quick reaction
                    messages=wrap_messages,  # type: ignore[arg-type]
                    max_tokens=toob_max_tokens,
                    temperature=0.9,
                )
                result = resp.choices[0].message.content
                if result:
                    return result
            except Exception as exc:
                log.warning("Toob personality wrap failed", error=str(exc)[:80])

        # Fallback: still use Toob voice for the raw music result
        return music_result

    async def _stream_toob_wrap_from_query(
        self, user_message: str, max_tokens: int,
    ) -> AsyncIterator[str]:
        """Stream Toob's reaction using only the user's request, not the
        music search result. Yields complete sentences as they form.

        Used by the speculative-wrap music path so TTS can start while
        ytdl search is still resolving in the background.
        """
        if not self.groq_api_key:
            return

        wrap_messages = [
            {
                "role": "system",
                "content": (
                    "You are Toob — a dark, malevolent spirit cursed to DJ for mortals. "
                    "You DESPISE every request put to you. You wish suffering on whoever "
                    "made it. Menacing, absurdly dramatic, like a demon working retail.\n"
                    "RULES:\n"
                    "- NEVER introduce yourself or say your name. Your voice IS your identity.\n"
                    "- ONE sentence. 6-10 words MAX. Tight, venomous.\n"
                    "- React to THE REQUEST (what the user asked for), not to yourself. "
                    "Insult their taste.\n"
                    "- No caps, no markdown, no emojis. Spoken aloud through TTS."
                ),
            },
            {"role": "user", "content": user_message},
            {
                "role": "user",
                "content": (
                    "React as Toob in ONE sentence (6-10 words). Menacing. "
                    "The user is requesting this — mock them for wanting it."
                ),
            },
        ]

        toob_max_tokens = min(max_tokens, 40)

        from groq import AsyncGroq

        client = AsyncGroq(api_key=self.groq_api_key)
        stream = await client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=wrap_messages,  # type: ignore[arg-type]
            max_tokens=toob_max_tokens,
            temperature=0.9,
            stream=True,
        )

        async def _chunks() -> AsyncIterator[str]:
            async for chunk in stream:
                if chunk.choices and chunk.choices[0].delta.content:
                    yield chunk.choices[0].delta.content

        async for sentence in _stream_sentences_from_chunks(_chunks()):
            yield sentence

    async def _handle_music_voice_streaming(
        self,
        original_message: str,
        user_id: str,
        max_tok: int,
        tool_args: dict | None,
        guild_id: int = 0,
    ) -> AsyncIterator[str]:
        """Speculative-wrap variant of _handle_music for voice + play.

        Runs the music handler (ytdl search + deferred queue) in the
        background while streaming a query-based Toob reaction. The
        user hears Toob within ~500ms instead of waiting 2-3s for
        search. The deferred-playback logic in the session starts
        music after TTS.

        `guild_id` is required for multi-guild isolation: the music
        handler dispatches to the correct guild's player; dedup is
        per-(guild, user); failure recovery is keyed the same way.
        """
        if self._music_handler is None:
            yield "music isn't set up right now"
            return

        # Hallucination guard — matches _handle_music. Drop play calls whose
        # query has zero lexical overlap with the current user message.
        if tool_args and tool_args.get("action") == "play":
            raw_query = (tool_args.get("query") or "").strip()
            query = self._scrub_music_query(raw_query)
            if query != raw_query:
                log.info(
                    "music.play query sanitized",
                    raw=raw_query[:80], scrubbed=query[:80],
                )
                tool_args = {**tool_args, "query": query}
            if len(query) < 2:
                log.info(
                    "music.play empty query — prompting user",
                    query=query, user=user_id,
                )
                yield "play what?"
                return
            if query:
                _STOPWORDS = {
                    "the", "a", "an", "by", "and", "or", "of", "to", "for",
                    "some", "any", "song", "songs", "music", "track", "play",
                    "put", "on", "it", "that", "this", "please", "can", "you",
                    "me", "us", "up",
                }
                msg_tokens = {
                    t for t in re.findall(r"[a-z0-9']+", original_message.lower())
                }
                query_tokens = {
                    t for t in re.findall(r"[a-z0-9']+", query.lower())
                    if t not in _STOPWORDS
                }
                if query_tokens and not (query_tokens & msg_tokens):
                    log.warning(
                        "music.play hallucinated from context — drop",
                        current_message=original_message[:120],
                        hallucinated_query=query[:80],
                        user=user_id,
                    )
                    return

            if self._is_duplicate_play(guild_id, user_id, query):
                log.warning(
                    "music.play duplicate suppressed",
                    query=query[:80], user=user_id, guild=guild_id,
                )
                return

        # Fan out music handler as a background task using THIS guild's id.
        music_task = asyncio.create_task(
            self._music_handler(
                original_message, int(user_id), guild_id,
                voice=True, tool_args=tool_args,
            )
        )

        play_query_for_dedup = (tool_args or {}).get("query") or ""

        def _on_music_task_done(t: asyncio.Task) -> None:
            try:
                resp = t.result()
            except Exception as exc:
                log.warning(
                    "Music handler (background) failed",
                    error=str(exc)[:120],
                )
                if play_query_for_dedup:
                    self._clear_play_on_failure(
                        guild_id, user_id, play_query_for_dedup,
                    )
                return
            log.info(
                "music.response", user=user_id,
                response=(resp or "")[:80],
            )
            if play_query_for_dedup and resp:
                success_marker = (
                    resp.startswith("Playing")
                    or resp.startswith("Queued")
                    or resp.startswith("[SILENT]")
                )
                if not success_marker:
                    self._clear_play_on_failure(
                        guild_id, user_id, play_query_for_dedup,
                    )

        music_task.add_done_callback(_on_music_task_done)

        # Concurrently stream the speculative wrap.
        wrap_text_parts: list[str] = []
        try:
            async for sentence in self._stream_toob_wrap_from_query(
                original_message, max_tok,
            ):
                wrap_text_parts.append(sentence)
                yield sentence
        except Exception as exc:
            log.warning("Speculative wrap failed", error=str(exc)[:80])

        # Non-blocking check: if music_task already finished while the
        # wrap streamed, surface a failure message. If it's still
        # running (the common case), we let it complete in the
        # background — the deferred-playback logic in the session will
        # start music when it's ready, and a total failure just means
        # the user doesn't hear music. Acceptable per the ~2% mismatch
        # trade-off documented in [[speculative-music-wrap]].
        if music_task.done():
            try:
                music_response = music_task.result()
            except Exception:
                yield "couldn't find it, chief"
                return
            if music_response:
                success = (
                    music_response.startswith("Playing")
                    or music_response.startswith("Queued")
                    or music_response.startswith("[SILENT]")
                )
                if not success:
                    yield "couldn't find it, chief"
                    return

        if wrap_text_parts:
            self._save_response(guild_id, user_id, " ".join(wrap_text_parts))

    async def _wrap_in_personality(
        self,
        user_message: str,
        deal_response: str,
        voice: bool,
        max_tokens: int,
        guild_id: int = 0,
    ) -> str:
        """Wrap a deal agent response in Poob's personality.

        Uses this guild's horniness level so the personality wrap
        respects multi-guild isolation.
        """
        level = self._horniness_for(guild_id) if voice else 5
        wrap_messages = [
            {
                "role": "system",
                "content": _build_system_prompt(level, voice=voice),
            },
            {"role": "user", "content": user_message},
            {
                "role": "assistant",
                "content": f"[My deal system says: {deal_response}]",
            },
            {
                "role": "user",
                "content": (
                    "Relay that to me in your style. Include ALL important info "
                    "(items, prices, questions asked, confirmations). Be concise."
                    + (" VOICE MODE: spoken aloud, no caps, no formatting." if voice else "")
                ),
            },
        ]

        if self.groq_api_key:
            try:
                from groq import AsyncGroq

                client = AsyncGroq(api_key=self.groq_api_key)
                resp = await client.chat.completions.create(
                    model=self.groq_model,
                    messages=wrap_messages,  # type: ignore[arg-type]
                    max_tokens=max_tokens,
                    temperature=0.8,
                )
                return resp.choices[0].message.content or deal_response
            except Exception as exc:
                log.warning("Personality wrap failed", error=str(exc)[:80])

        # Fallback: return deal response as-is
        return deal_response

    async def _stream_personality_wrap(
        self,
        user_id: str,
        user_message: str,
        deal_response: str,
        max_tokens: int,
        guild_id: int = 0,
    ) -> AsyncIterator[str]:
        """Stream a personality-wrapped deal response sentence by sentence."""
        wrap_messages = [
            {
                "role": "system",
                "content": _build_system_prompt(
                    self._horniness_for(guild_id), voice=True,
                ),
            },
            {"role": "user", "content": user_message},
            {
                "role": "assistant",
                "content": f"[My deal system says: {deal_response}]",
            },
            {
                "role": "user",
                "content": (
                    "Relay that to me in your style. Include ALL important info. "
                    "VOICE MODE: spoken aloud, no caps, no formatting."
                ),
            },
        ]

        full_response = ""
        buffer = ""

        try:
            async for token in self._groq_stream(wrap_messages, max_tokens):
                buffer += token
                if SENTENCE_END.search(buffer):
                    sentence = buffer.strip()
                    if sentence:
                        full_response += sentence + " "
                        yield sentence
                    buffer = ""
            if buffer.strip():
                full_response += buffer.strip()
                yield buffer.strip()
        except Exception as exc:
            log.warning("Stream wrap failed", error=str(exc)[:80])
            yield deal_response
            full_response = deal_response

        self._save_response(user_id, full_response.strip())

    # ------------------------------------------------------------------
    # LLM providers
    # ------------------------------------------------------------------

    async def _groq_with_tools(
        self, messages: list[dict], max_tokens: int,
    ) -> tuple[str, str | None, dict | None] | None:
        """Call Groq with deal_assistant and music_assistant tools.

        Retries with fallback model on 400 errors (common with short messages
        on certain Groq models). The LLM decides intent — no keyword matching.

        Returns:
            (text, tool_name, tool_args) on success, None on failure.
            tool_name/tool_args are None if no tool was called.
        """
        tools = [DEAL_TOOL]
        if self._music_handler is not None:
            tools.append(MUSIC_TOOL)

        # Multi-provider tool-calling cascade. See docs/decisions for the
        # rationale and docs/references/vlm-cascade-operational-findings.
        #
        # Cerebras qwen-3-235b used to sit between Groq and NVIDIA but was
        # chronically 429-ed in production and added ~200-300ms of dead
        # retry tax without ever serving a successful call. Dropped.
        # Re-enable only if Cerebras lifts the rate limit AND a benchmark
        # shows a latency win over NVIDIA.

        providers = []
        # 1. Groq primary (openai/gpt-oss-20b by default; see
        #    decisions/groq-gpt-oss-20b-swap). The <function=...> recovery
        #    regex in _call_provider_with_tools handles the known
        #    llama-3.3-70b parser regression if that model is ever set.
        if self.groq_api_key:
            providers.append(("groq", self.groq_model))
        # 2. NVIDIA NIM — different provider, sidesteps Groq rate limits.
        if self.nvidia_api_key:
            providers.append(("nvidia", self.nvidia_model))
        # 4. Groq Scout 17B — LAST resort only. Fast but routes too aggressively
        #    to music_assistant. Only used when all other providers are down.
        if self.groq_api_key:
            providers.append(("groq", "meta-llama/llama-4-scout-17b-16e-instruct"))

        # High-signal tool indicators: if the user's last message contains
        # these, a text-only response is almost certainly wrong. Force the
        # cascade to keep trying providers until one calls a tool.
        user_msg = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                user_msg = (m.get("content") or "").lower()
                break
        tool_signals = (
            "wishlist", "watchlist", "my list", "on my list", "show my",
            "what am i", "add to", "remove from", "clear ", "scan",
            "deals", "listings", "find me", "watching for", "looking for",
        )
        looks_tool_worthy = any(sig in user_msg for sig in tool_signals)

        last_text = ""
        last_tool: str | None = None
        last_args: dict | None = None

        # Tool-call emission has its own token budget (see
        # _TOOL_DETECTION_MAX_TOKENS). Independent of the response cap,
        # which only governs casual streaming length. Conflating them
        # caused gpt-oss-20b to truncate mid-arguments at 80 tokens
        # and trigger Groq tool_use_failed 400s.
        tool_max_tokens = max(max_tokens, _TOOL_DETECTION_MAX_TOKENS)

        for idx, (provider, model) in enumerate(providers):
            is_last = idx == len(providers) - 1
            try:
                text, tool_name, tool_args = await self._call_provider_with_tools(
                    provider, model, messages, tools, tool_max_tokens,
                )
                if tool_name:
                    log.info("poob.tool_route", tool=tool_name,
                             provider=provider, model=model,
                             args=str(tool_args)[:100] if tool_args else "")
                    return text, tool_name, tool_args

                # No tool call. If the message looks tool-worthy and we have
                # more providers to try, keep going — this one may have
                # misinterpreted a clear tool query as casual chat.
                if looks_tool_worthy and not is_last:
                    log.info(
                        "poob.no_tool_but_tool_worthy_trying_next",
                        provider=provider, model=model,
                    )
                    last_text, last_tool, last_args = text, tool_name, tool_args
                    continue

                return text, tool_name, tool_args

            except Exception as exc:
                if not is_last:
                    log.warning("Tool detection failed, trying next",
                                provider=provider, model=model, error=str(exc)[:500])
                    continue
                # All providers exhausted — return last text we have (or raise)
                if last_text:
                    return last_text, last_tool, last_args
                raise

        # All providers tried, none called a tool — return last text response
        return last_text, last_tool, last_args

    @staticmethod
    def _recover_tool_call_from_groq_400(
        exc: Exception, model: str,
    ) -> tuple[str, str, dict] | None:
        """Parse a Groq 400 `tool_use_failed` error for an embedded tool
        call. Returns (text, tool_name, tool_args) on success, or None.

        Known regression: some Groq-hosted models emit Meta's native
        `<function=name>{...}</function>` wrapper instead of the structured
        `tool_calls` array. Groq's parser rejects it; the raw output is
        preserved in `failed_generation`.
        """
        import json as _json
        import re as _re
        body = getattr(exc, "body", None) or {}
        err = body.get("error", {}) if isinstance(body, dict) else {}
        if err.get("code") != "tool_use_failed":
            return None
        failed = err.get("failed_generation") or ""
        if not isinstance(failed, str) or "<function" not in failed:
            return None
        name_match = _re.search(r"<function\s*=\s*([a-zA-Z0-9_-]+)", failed)
        if not name_match:
            return None
        tool_name = name_match.group(1)
        brace_start = failed.find("{", name_match.end())
        if brace_start < 0:
            return None
        try:
            args, _consumed = _json.JSONDecoder().raw_decode(failed[brace_start:])
        except ValueError:
            return None
        if not isinstance(args, dict):
            return None
        log.info(
            "groq.tool_call_recovered_from_function_tag",
            model=model, tool=tool_name,
        )
        return "", tool_name, args

    async def _call_provider_with_tools(
        self,
        provider: str,
        model: str,
        messages: list[dict],
        tools: list[dict],
        max_tokens: int,
    ) -> tuple[str, str | None, dict | None]:
        """Call a single provider with tool definitions.

        Normalizes response format across all providers into a clean
        (text, tool_name, tool_args) tuple. No fake SDK objects, no format hacks.

        Args:
            provider: "groq", "cerebras", or "nvidia".
            model: Model identifier for the provider.
            messages: Chat messages.
            tools: Tool definitions.
            max_tokens: Max response tokens.

        Returns:
            (text_content, tool_name, tool_args) — tool_name/tool_args are None
            if no tool was called.
        """
        if provider == "groq":
            import json as _json
            import re as _re
            from groq import AsyncGroq
            from groq import BadRequestError as _GroqBadRequest
            client = AsyncGroq(api_key=self.groq_api_key)
            try:
                response = await client.chat.completions.create(
                    model=model,
                    messages=messages,  # type: ignore[arg-type]
                    tools=tools,  # type: ignore[arg-type]
                    tool_choice="auto",
                    max_tokens=max_tokens,
                    temperature=0.8,
                )
            except _GroqBadRequest as exc:
                # Recover from Groq's tool_use_failed parser regression —
                # some models emit <function=name>{...}</function> which
                # Groq's OpenAI-compat parser cannot convert. Parse the
                # raw failed_generation client-side.
                recovered = self._recover_tool_call_from_groq_400(exc, model)
                if recovered is not None:
                    return recovered
                raise
            choice = response.choices[0]
            if choice.message.tool_calls:
                tc = choice.message.tool_calls[0]
                try:
                    args = _json.loads(tc.function.arguments)
                except (ValueError, TypeError):
                    args = {}
                return "", tc.function.name, args
            return choice.message.content or "", None, None

        # Cerebras and NVIDIA use OpenAI-compatible REST APIs
        if provider == "cerebras":
            url = "https://api.cerebras.ai/v1/chat/completions"
            headers = {"Authorization": f"Bearer {self.cerebras_api_key}"}
            body = {
                "model": model, "messages": messages,
                "tools": tools, "tool_choice": "auto",
                "max_tokens": max_tokens, "temperature": 0.8,
            }
        elif provider == "nvidia":
            url = "https://integrate.api.nvidia.com/v1/chat/completions"
            headers = {"Authorization": f"Bearer {self.nvidia_api_key}"}
            body = {
                "model": model, "messages": messages,
                "tools": tools, "max_tokens": max_tokens,
            }
        else:
            raise ValueError(f"Unknown provider: {provider}")

        async with httpx.AsyncClient(timeout=15.0) as http:
            r = await http.post(url, headers=headers, json=body)
            r.raise_for_status()
            data = r.json()

        import json as _json

        msg = data["choices"][0]["message"]
        tool_calls = msg.get("tool_calls")
        if tool_calls and len(tool_calls) > 0:
            tc = tool_calls[0]
            raw_args = tc["function"].get("arguments", "{}")
            try:
                args = _json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            except (ValueError, TypeError):
                args = {}
            return "", tc["function"]["name"], args
        return msg.get("content", "") or "", None, None

    async def _groq_stream(
        self, messages: list[dict], max_tokens: int,
    ) -> AsyncIterator[str]:
        """Stream tokens from Groq (no tools)."""
        from groq import AsyncGroq

        client = AsyncGroq(api_key=self.groq_api_key)
        stream = await client.chat.completions.create(
            model=self.groq_model,
            messages=messages,  # type: ignore[arg-type]
            max_tokens=max_tokens,
            temperature=0.8,
            stream=True,
        )
        async for chunk in stream:
            if chunk.choices and chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content

    async def _fallback_generate(
        self, messages: list[dict], max_tokens: int,
    ) -> str:
        """Fallback generation without tool calling (Cerebras → Ollama)."""
        if self.cerebras_api_key:
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.post(
                        "https://api.cerebras.ai/v1/chat/completions",
                        headers={
                            "Authorization": f"Bearer {self.cerebras_api_key}",
                            "Content-Type": "application/json",
                        },
                        json={
                            "model": self.cerebras_model,
                            "messages": messages,
                            "max_tokens": max_tokens,
                            "temperature": 0.8,
                        },
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    content = data["choices"][0]["message"]["content"]
                    if content:
                        return content
            except Exception as exc:
                log.warning("Cerebras fallback failed", error=str(exc)[:100])

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    f"{self.ollama_base_url}/api/chat",
                    json={
                        "model": self.ollama_model,
                        "messages": messages,
                        "stream": False,
                        "options": {
                            "num_predict": max_tokens,
                            "temperature": 0.8,
                        },
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                content = data.get("message", {}).get("content", "")
                if content:
                    return content
        except Exception as exc:
            log.warning("Ollama fallback failed", error=str(exc)[:100])

        return "Brain's offline. Try again in a sec."
