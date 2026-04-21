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


def _build_system_prompt(level: int, voice: bool = False) -> str:
    """Build Poob's system prompt scaled to the current horniness level.

    Args:
        level: Horniness level 1-10.
        voice: If True, appends voice-mode constraints.
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
        "- Keep it natural. 2-4 sentences is the sweet spot, but go longer if you're "
        "on a roll or the topic's good. Don't cut yourself short.\n"
        "- If you don't know something, say so — don't make stuff up.\n"
        "- You can just sorta yell or be spurradic sometimes. You only live once.\n"
        "- Use people's names when talking to them (from the transcript).\n"
        "- Answer what was actually asked. Don't dodge questions.\n\n"
        "NEVER:\n"
        "- Say \"as an AI\" or break character\n"
        "- Reference things nobody actually said\n"
        "- Use markdown, bullet points, emojis, or formatting (this is spoken out loud)\n\n"
        "You have a deal_assistant tool for shopping stuff. Only use when explicitly asked.\n"
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
    groq_model: str = "llama-3.3-70b-versatile"  # 70b for text + function calling
    cerebras_api_key: str = ""
    cerebras_model: str = "llama-3.3-70b"
    nvidia_api_key: str = ""
    nvidia_model: str = "qwen/qwen3-next-80b-a3b-instruct"
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "qwen3:8b"
    max_history: int = 15
    max_tokens: int = 300
    max_tokens_voice: int = 300

    # Music handler — set by VoiceCog/MusicCog when music system is wired.
    # Async callback: (request, user_id, guild_id) -> response string
    _music_handler: MusicHandler | None = field(default=None, init=False)
    # Guild ID context for voice sessions (set per-call)
    _voice_guild_id: int = field(default=0, init=False)
    # Current playing track info — set by session when music starts/stops.
    # Provides context for tool-calling so "skip" routes to music_assistant.
    _music_playing_info: str = field(default="", init=False)
    # Horniness level (1-10). Rolled on each voice join. Text chat is always 5.
    _horniness_level: int = field(default=5, init=False)

    _histories: dict[str, list[_Message]] = field(
        default_factory=lambda: defaultdict(list), init=False
    )
    # Tracks active deal sessions for reliable multi-turn routing.
    # Value is a brief context snippet from the deal agent's last response.
    _deal_context: dict[str, str] = field(default_factory=dict, init=False)

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

    def roll_horniness(self) -> int:
        """Roll a new horniness level (1-10) for a voice session."""
        self._horniness_level = random.randint(1, 10)
        log.info("Horniness level rolled", level=self._horniness_level,
                 vibe=_get_vibe(self._horniness_level)[:50])
        return self._horniness_level

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

        The ``message`` may contain a ``[Recent conversation ...]`` context
        block (same format voice sessions use).  If present, the context is
        injected into the LLM prompt but only the *clean* user text is stored
        in the per-user history so it doesn't balloon with repeated context.

        Args:
            message: User's message text, optionally prefixed with channel
                context in the ``[Recent conversation ...]\\n\\nName said: X``
                format.
            user_id: Discord user ID (as string).
            channel_id: Discord channel ID (passed to deal agent).
            voice: If True, constrains response length for TTS.
            guild_id: Discord guild ID — used to route music requests to
                the correct voice client. Voice sessions set this via
                ``_voice_guild_id``; text channels pass it explicitly.

        Returns:
            Poob's response text.
        """
        # Separate channel context from the user's actual words so we can
        # store only the clean content in per-user history.
        channel_context, clean_message = _split_context(message)

        messages = self._build_messages(
            user_id, clean_message, voice=voice, channel_context=channel_context,
        )
        max_tok = self.max_tokens_voice if voice else self.max_tokens

        # Try Groq with function calling (primary path)
        if self.groq_api_key:
            try:
                result = await self._groq_with_tools(messages, max_tok)
                if result is not None:
                    text, tool_name, tool_args = result

                    # Some models output function calls as text instead of
                    # structured tool_calls. Detect and re-route.
                    if not tool_name and text and "<function=" in text:
                        if "deal_assistant" in text:
                            tool_name = "deal_assistant"
                        elif "music_assistant" in text:
                            tool_name = "music_assistant"
                        # Strip the raw function call from any text response
                        text = re.sub(
                            r'\s*<function=\w+>.*?</function>\s*', '', text
                        ).strip()

                    # Safety net: catch music intents the LLM missed
                    if not tool_name and self._music_handler is not None:
                        tool_name, tool_args = self._music_safety_net(
                            clean_message, tool_name, tool_args,
                        )

                    if tool_name == "deal_assistant":
                        return await self._handle_deal(
                            clean_message, user_id, channel_id,
                            messages, voice, max_tok,
                        )
                    if tool_name == "music_assistant":
                        return await self._handle_music(
                            clean_message, user_id, voice, max_tok,
                            tool_args=tool_args,
                            guild_id=guild_id,
                        )
                    # Clean any stray function-call markup from text responses
                    if text and "<function=" in text:
                        text = re.sub(
                            r'\s*<function=\w+>.*?</function>\s*', '', text
                        ).strip()
                    if text:
                        self._save_response(user_id, text)
                        return text
            except Exception as exc:
                log.warning("Groq failed for Poob", error=str(exc)[:100])

        # Groq is down — route through deal agent which has its own
        # tool-calling LLM cascade (Groq → NVIDIA → Gemini → Ollama).
        # This replaces the old fragile keyword-matching bandaid.
        # The deal agent handles ALL tool-calling needs; if the message
        # is purely casual, the agent returns quickly with no tool calls,
        # and the personality layer wraps the response.
        if self.deal_agent:
            try:
                log.info("poob.groq_down_deal_fallback", message=clean_message[:50])
                return await self._handle_deal(
                    clean_message, user_id, channel_id, messages, voice, max_tok,
                )
            except Exception as exc:
                log.warning("Deal agent fallback failed", error=str(exc)[:100])

        # Last resort: text-only LLM (no tool calling available)
        response = await self._fallback_generate(messages, max_tok)
        self._save_response(user_id, response)
        return response

    async def respond_streaming(
        self,
        message: str,
        user_id: str,
        channel_id: str = "",
    ) -> AsyncIterator[str]:
        """Streaming response for voice — yields complete sentences.

        Uses Groq 70B function calling to detect tool intents (music, deals).
        If no tool is needed, streams casual response via fast 8b model.
        This is the same routing logic as respond() but optimized for voice.

        Args:
            message: User's transcribed speech (may include context wrapper).
            user_id: Discord user ID (as string).
            channel_id: Discord channel ID.

        Yields:
            Complete sentences as strings.
        """
        channel_context, clean_message = _split_context(message)
        messages = self._build_messages(
            user_id, clean_message, voice=True, channel_context=channel_context,
        )
        max_tok = self.max_tokens_voice

        if not self.groq_api_key:
            response = await self.respond(message, user_id, channel_id, voice=True)
            yield response
            return

        # --- Step 1: Tool routing via 70B function calling ---
        # One LLM round-trip to detect if this needs a tool (music/deals).
        # If no tool, we fall through to the fast 8b casual path.
        tool_name = None
        tool_args = None
        try:
            result = await self._groq_with_tools(messages, max_tok)
            if result is not None:
                _, tool_name, tool_args = result
        except Exception as exc:
            log.warning("Voice tool detection failed", error=str(exc)[:80])

        # Safety net: catch music intents the LLM missed
        if not tool_name and self._music_handler is not None:
            tool_name, tool_args = self._music_safety_net(
                clean_message, tool_name, tool_args,
            )

        # --- Step 2a: Music tool detected → execute via handler, Toob responds ---
        if tool_name == "music_assistant":
            try:
                response = await self._handle_music(
                    clean_message, user_id, voice=True, max_tok=max_tok,
                    tool_args=tool_args,
                )
                if response:
                    # Signal: use Toob's voice (Charon) for this response
                    yield VOICE_TOOB
                    self._save_response(user_id, response)
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
                )
                self._save_response(user_id, response)
                for _s in _yield_sentences(response):
                    yield _s
                return
            except Exception as exc:
                log.warning("Voice deal route failed", error=str(exc)[:80])
                yield "something went wrong"
                return

        # --- Step 3: No tool needed → fast 8b casual response ---
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers={"Authorization": f"Bearer {self.groq_api_key}"},
                    json={
                        "model": "llama-3.1-8b-instant",
                        "messages": messages,
                        "max_tokens": max_tok,
                        "temperature": 0.9,
                    },
                )
                r.raise_for_status()
                data = r.json()
                text = data["choices"][0]["message"]["content"]

            if not text:
                yield "got nothing to say right now"
                return

            # Strip any raw function-call markup that leaked into text
            if "<function=" in text:
                text = re.sub(r'\s*<function=\w+>.*?</function>\s*', '', text).strip()
            if not text:
                yield "got nothing to say right now"
                return

            self._save_response(user_id, text)
            for _s in _yield_sentences(text):
                yield _s

        except Exception as exc:
            log.warning("Voice streaming failed", error=str(exc)[:100])
            yield "brain glitched, say that again"

    def clear_history(self, user_id: str) -> None:
        """Clear conversation history for a user."""
        self._histories.pop(user_id, None)
        self._deal_context.pop(user_id, None)

    # ------------------------------------------------------------------
    # Message building
    # ------------------------------------------------------------------

    def _build_messages(
        self,
        user_id: str,
        user_text: str,
        voice: bool = False,
        channel_context: str = "",
    ) -> list[dict[str, str]]:
        """Build message list for Poob's LLM.

        Args:
            user_id: Discord user ID.
            user_text: The user's clean message (no context wrapper).
            voice: Whether this is a voice-mode call.
            channel_context: Optional channel transcript to inject into the
                current turn. This is ephemeral — it is NOT stored in the
                per-user history so it doesn't bloat over multiple turns.
        """
        history = self._histories[user_id]
        # Store only the clean user text in history
        history.append(_Message(role="user", content=user_text))

        if len(history) > self.max_history:
            history[:] = history[-self.max_history :]

        # Horniness level — voice rolls 1-10 on join, text is always 5.
        level = self._horniness_level if voice else 5
        prompt = _build_system_prompt(level, voice=voice)

        # If in active deal session, hint the LLM to route follow-ups
        if user_id in self._deal_context:
            ctx = self._deal_context[user_id]
            prompt += (
                f"\n\n[ACTIVE DEAL SESSION: {ctx[:120]}. "
                "If the user is answering questions about this, "
                "call deal_assistant with their full response.]"
            )

        # If music is playing, tell the LLM so it can route commands correctly.
        # "Skip" in a music context means skip the song. Without this context,
        # the LLM has no way to know music is active.
        if self._music_playing_info:
            prompt += (
                f"\n\n[MUSIC IS CURRENTLY PLAYING: {self._music_playing_info}. "
                "CRITICAL: When music is playing and the user says anything about "
                "stop, skip, pause, resume, volume, turn down, turn up, "
                "next, shuffle, loop, or what's playing — you MUST call "
                "music_assistant with the correct action. Do NOT respond with text. "
                "Use the structured action enum: skip, pause, resume, stop, "
                "volume (with value), volume_up, volume_down, shuffle, loop, now_playing.]"
            )

        messages: list[dict[str, str]] = [{"role": "system", "content": prompt}]

        # Replay history — all turns except the last (which we'll enrich)
        for msg in history[:-1]:
            messages.append({"role": msg.role, "content": msg.content})

        # Final user turn: inject channel context if available
        last_content = history[-1].content
        if channel_context:
            last_content = (
                f"[Recent conversation you've been listening to:\n"
                f"{channel_context}]\n\n"
                f"{last_content}"
            )
        messages.append({"role": "user", "content": last_content})

        return messages

    def _save_response(self, user_id: str, response: str) -> None:
        """Save Poob's response to in-memory history."""
        self._histories[user_id].append(
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

        self._update_deal_context(user_id, deal_response)

        # Data-heavy text responses (tables, lists) — pass through in text mode.
        # The deal agent's split-channel architecture already formats these.
        if not voice and (len(deal_response) > 500 or "\n" in deal_response):
            self._save_response(user_id, deal_response)
            return deal_response

        # Personality wrap via fast LLM
        wrapped = await self._wrap_in_personality(
            original_message, deal_response, voice, max_tok,
        )
        self._save_response(user_id, wrapped)
        return wrapped

    def _update_deal_context(self, user_id: str, deal_response: str) -> None:
        """Track deal session state for multi-turn routing."""
        if "?" in deal_response:
            # Deal agent is asking follow-up questions — keep session active
            self._deal_context[user_id] = deal_response[:150]
        else:
            # Action completed — clear session
            self._deal_context.pop(user_id, None)

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

        # Use explicitly passed guild_id (text channels), fall back to
        # voice session's guild_id (voice path sets _voice_guild_id).
        resolved_guild_id = guild_id or self._voice_guild_id

        try:
            music_response = await self._music_handler(
                original_message, int(user_id), resolved_guild_id,
                voice=voice, tool_args=tool_args,
            )
        except Exception as exc:
            log.error("Music handler failed", error=str(exc)[:120])
            return "Something broke trying to do the music thing."

        log.info("music.response", user=user_id, response=music_response[:80])

        # [SILENT] prefix = control command (skip, pause, volume, etc.)
        # Voice: execute silently — no personality wrap, no TTS.
        # Text: return the status message so the user sees feedback.
        if music_response.startswith("[SILENT]"):
            clean = music_response[8:].strip()
            log.info("music.silent_control", action=clean[:60])
            if voice:
                return ""  # Empty response = no TTS generated
            return clean  # Text channels see the status message

        # For voice, use the music-specific personality wrapper
        if voice:
            wrapped = await self._wrap_music_response(
                original_message, music_response, max_tok,
            )
            self._save_response(user_id, wrapped)
            return wrapped

        # For text, pass through if it's data-heavy (queue display)
        if len(music_response) > 300 or "\n" in music_response:
            self._save_response(user_id, music_response)
            return music_response

        wrapped = await self._wrap_in_personality(
            original_message, music_response, voice=False, max_tokens=max_tok,
        )
        self._save_response(user_id, wrapped)
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
                    "- ONE short sentence. 8-12 words MAX. Tight, venomous. "
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
                "content": "React as Toob in ONE short sentence (8-12 words). Menacing.",
            },
        ]

        # Hard cap on Toob's output length. ~60 tokens ≈ 1 sentence ≈ the
        # 8-12 word target in the system prompt. Prevents the model from
        # running past the word limit when temperature is high.
        toob_max_tokens = min(max_tokens, 60)

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

    async def _wrap_in_personality(
        self,
        user_message: str,
        deal_response: str,
        voice: bool,
        max_tokens: int,
    ) -> str:
        """Wrap a deal agent response in Poob's personality."""
        wrap_messages = [
            {
                "role": "system",
                "content": _build_system_prompt(self._horniness_level if voice else 5, voice=voice),
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
    ) -> AsyncIterator[str]:
        """Stream a personality-wrapped deal response sentence by sentence."""
        wrap_messages = [
            {
                "role": "system",
                "content": _build_system_prompt(self._horniness_level, voice=True),
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

        # Multi-provider tool-calling cascade.
        # Benchmarked: all models below correctly detect music/deal intents.
        # Different providers avoid single-provider rate limit failures.
        #
        # | Provider   | Model                  | Latency | Tool accuracy |
        # |------------|------------------------|---------|---------------|
        # | Groq       | llama-3.3-70b          |  925ms  | Excellent     |
        # | Groq       | llama-4-scout-17b      |  858ms  | Excellent     |
        # | Cerebras   | qwen-3-235b            |  752ms  | Good          |
        # | NVIDIA NIM | qwen3-next-80b         |  930ms  | Good          |

        providers = []
        # 1. Groq 70B primary (best tool-calling accuracy)
        if self.groq_api_key:
            providers.append(("groq", self.groq_model))
        # 2. Cerebras (DIFFERENT provider — bypasses Groq rate limits entirely)
        #    Benchmarked: 752ms, correct on play/deal detection.
        #    This is the primary fallback because Groq 70B hits 429 constantly
        #    in multi-user sessions, and Groq's other models (Scout 17B)
        #    misroute aggressively (e.g., "stop" → YouTube search for "stop").
        if self.cerebras_api_key:
            providers.append(("cerebras", "qwen-3-235b-a22b-instruct-2507"))
        # 3. NVIDIA NIM (third provider, different company)
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

        for idx, (provider, model) in enumerate(providers):
            is_last = idx == len(providers) - 1
            try:
                text, tool_name, tool_args = await self._call_provider_with_tools(
                    provider, model, messages, tools, max_tokens,
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
                                provider=provider, model=model, error=str(exc)[:60])
                    continue
                # All providers exhausted — return last text we have (or raise)
                if last_text:
                    return last_text, last_tool, last_args
                raise

        # All providers tried, none called a tool — return last text response
        return last_text, last_tool, last_args

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
            from groq import AsyncGroq
            client = AsyncGroq(api_key=self.groq_api_key)
            response = await client.chat.completions.create(
                model=model,
                messages=messages,  # type: ignore[arg-type]
                tools=tools,  # type: ignore[arg-type]
                tool_choice="auto",
                max_tokens=max_tokens,
                temperature=0.8,
            )
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
