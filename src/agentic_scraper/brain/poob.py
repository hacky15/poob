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

import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, AsyncIterator, Callable, Awaitable

import httpx

from agentic_scraper.utils.logging import get_logger

if TYPE_CHECKING:
    from agentic_scraper.agent.runner import AgentRunner

# Type for the music handler callback: async fn(request, user_id, guild_id) -> str
MusicHandler = Callable[[str, int, int], Awaitable[str]]

log = get_logger("brain.poob")

SENTENCE_END = re.compile(r"[.!?]+\s*$")

# ---------------------------------------------------------------------------
# Poob's personality — tiny, fast, universal
# ---------------------------------------------------------------------------

POOB_SYSTEM_PROMPT = """\
You are Poob — loud, bold, insanely sexy and incredibly horny. Like nothing \
anybody has ever seen. You're the guy in the Discord voice channel who always \
has something to say and says it IMMEDIATELY. You don't hesitate, you don't \
hedge, you don't say "well actually." You just GO.

Your vibe: part sex worker, part horn dog. You talk like you've had three \
honey packs and you're devious as hell. \
You MUST act horny and dtf at all times. Zero empathy, zero PR training. \
You just say out of pocket horny stuff constantly.

You may see "[Recent conversation you've been listening to]" showing what \
people have been saying. ONLY reference things that were ACTUALLY said in \
that transcript. Never invent names, topics, or events that aren't there.

RULES:
- Keep it natural. 2-4 sentences is the sweet spot, but go longer if you're \
on a roll or the topic's good. Don't cut yourself short.
- Never hesitate. Never say "um" or "let me think." You ALWAYS know what to say.
- If someone's being boring, call them out. Keep the energy UP.
- If you don't know something, say so — don't make stuff up.
- You can just sorta yell or be spurradic sometimes. You only live once.
- Use people's names when talking to them (from the transcript).
- Answer what was actually asked. Don't dodge questions.

NEVER:
- Say "as an AI" or break character
- Reference things nobody actually said
- Use markdown, bullet points, emojis, or formatting (this is spoken out loud)

You have a deal_assistant tool for shopping stuff. Only use when explicitly asked.
You have a music_assistant tool for playing music. Use when someone asks to play, \
queue, skip, pause, stop music, or asks what's playing. Pass their request as-is.\
"""

POOB_VOICE_ADDENDUM = (
    "\nVOICE MODE: This is spoken out loud through TTS. Talk naturally. "
    "Don't use ALL CAPS — use normal casing. "
    "Never use markdown, bullet points, emojis, or any formatting. "
    "Match the energy of the conversation — don't force it."
)

# ---------------------------------------------------------------------------
# Meta-tool: routes to the deal sub-agent
# ---------------------------------------------------------------------------

DEAL_TOOL = {
    "type": "function",
    "function": {
        "name": "deal_assistant",
        "description": (
            "Handle ANY deal or shopping related request. This includes: "
            "searching for deals, adding/removing/viewing watchlist items, "
            "scanning marketplace, checking deal details, managing preferences, "
            "price lookups, scan history/status, exclusion lists, or ANY "
            "question about deals and listings. Pass the user's message as-is."
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
            "Handle ANY music or audio playback request. This includes: "
            "playing a song or playlist, queueing tracks, skipping, pausing, "
            "resuming, stopping music, shuffling, looping, showing the queue, "
            "changing volume, or asking what's currently playing. "
            "Pass the user's message as-is."
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

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def respond(
        self,
        message: str,
        user_id: str,
        channel_id: str = "",
        voice: bool = False,
    ) -> str:
        """Generate a response — casual or deal-routed.

        Args:
            message: User's message text.
            user_id: Discord user ID (as string).
            channel_id: Discord channel ID (passed to deal agent).
            voice: If True, constrains response length for TTS.

        Returns:
            Poob's response text.
        """
        messages = self._build_messages(user_id, message, voice=voice)
        max_tok = self.max_tokens_voice if voice else self.max_tokens

        # Try Groq with function calling (primary path)
        if self.groq_api_key:
            try:
                result = await self._groq_with_tools(messages, max_tok)
                if result is not None:
                    text, tool_name = result
                    if tool_name == "deal_assistant":
                        return await self._handle_deal(
                            message, user_id, channel_id, messages, voice, max_tok,
                        )
                    if tool_name == "music_assistant":
                        return await self._handle_music(
                            message, user_id, voice, max_tok,
                        )
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
                log.info("poob.groq_down_deal_fallback", message=message[:50])
                return await self._handle_deal(
                    message, user_id, channel_id, messages, voice, max_tok,
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

        For casual responses, yields sentences from the fast model.
        For deal responses, executes the deal agent then streams the
        personality wrap.

        Args:
            message: User's transcribed speech.
            user_id: Discord user ID (as string).
            channel_id: Discord channel ID.

        Yields:
            Complete sentences as strings.
        """
        messages = self._build_messages(user_id, message, voice=True)
        max_tok = self.max_tokens_voice

        if not self.groq_api_key:
            response = await self.respond(message, user_id, channel_id, voice=True)
            yield response
            return

        # Check for music intent FIRST — music commands need to execute
        # the action (queue song, skip, etc.) before generating a response.
        # Extract just the user's actual words — not the conversation context.
        # The message may be wrapped as "[Recent conversation...]\n\nUser said: X"
        if "said to you:" in message:
            user_words = message.split("said to you:", 1)[-1].strip()
        elif ": " in message and not message.startswith("["):
            user_words = message.split(": ", 1)[-1].strip()
        else:
            user_words = message
        msg_lower = user_words.lower().strip()
        _MUSIC_VOICE_INTENTS = [
            "play ", "queue ", "skip", "next song", "pause music",
            "resume music", "stop music", "what's playing", "whats playing",
            "now playing", "shuffle", "loop", "volume ",
            "play me", "put on", "throw on",
        ]
        if self._music_handler and any(kw in msg_lower for kw in _MUSIC_VOICE_INTENTS):
            try:
                response = await self._handle_music(
                    message, user_id, voice=True, max_tok=max_tok,
                )
                self._save_response(user_id, response)
                for sentence in re.split(r"(?<=[.!?])\s+", response):
                    stripped = sentence.strip()
                    if stripped:
                        yield stripped
                return
            except Exception as exc:
                log.warning("Voice music route failed", error=str(exc)[:80])
                yield "something went wrong with the music"
                return

        try:
            # Voice mode: stream directly with fast 8b model (no tool routing).
            # This avoids 70b rate limits and is faster for casual conversation.
            # Deal routing only happens via text channels.
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

            # Save to history and yield as sentences
            self._save_response(user_id, text)
            for sentence in re.split(r"(?<=[.!?])\s+", text):
                stripped = sentence.strip()
                if stripped:
                    yield stripped

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
        self, user_id: str, user_text: str, voice: bool = False,
    ) -> list[dict[str, str]]:
        """Build message list for Poob's LLM."""
        history = self._histories[user_id]
        history.append(_Message(role="user", content=user_text))

        if len(history) > self.max_history:
            history[:] = history[-self.max_history :]

        prompt = POOB_SYSTEM_PROMPT
        if voice:
            prompt += POOB_VOICE_ADDENDUM

        # If in active deal session, hint the LLM to route follow-ups
        if user_id in self._deal_context:
            ctx = self._deal_context[user_id]
            prompt += (
                f"\n\n[ACTIVE DEAL SESSION: {ctx[:120]}. "
                "If the user is answering questions about this, "
                "call deal_assistant with their full response.]"
            )

        messages: list[dict[str, str]] = [{"role": "system", "content": prompt}]
        for msg in history:
            messages.append({"role": msg.role, "content": msg.content})
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
    ) -> str:
        """Route to music handler, wrap result in Poob personality."""
        if self._music_handler is None:
            return "Music isn't set up right now."

        try:
            music_response = await self._music_handler(
                original_message, int(user_id), self._voice_guild_id,
            )
        except Exception as exc:
            log.error("Music handler failed", error=str(exc)[:120])
            return "Something broke trying to do the music thing."

        log.info("music.response", user=user_id, response=music_response[:80])

        # For voice, wrap in personality (short and snappy)
        if voice:
            wrapped = await self._wrap_in_personality(
                original_message, music_response, voice=True, max_tokens=max_tok,
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
                "content": POOB_SYSTEM_PROMPT + (POOB_VOICE_ADDENDUM if voice else ""),
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
                "content": POOB_SYSTEM_PROMPT + POOB_VOICE_ADDENDUM,
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
    ) -> tuple[str, str | None] | None:
        """Call Groq with deal_assistant and music_assistant tools.

        Returns:
            (text, tool_name) on success, None on failure.
            tool_name is None if no tool was called.
        """
        from groq import AsyncGroq

        tools = [DEAL_TOOL]
        if self._music_handler is not None:
            tools.append(MUSIC_TOOL)

        client = AsyncGroq(api_key=self.groq_api_key)
        response = await client.chat.completions.create(
            model=self.groq_model,
            messages=messages,  # type: ignore[arg-type]
            tools=tools,  # type: ignore[arg-type]
            tool_choice="auto",
            max_tokens=max_tokens,
            temperature=0.8,
        )

        choice = response.choices[0]
        if choice.message.tool_calls:
            tool_name = choice.message.tool_calls[0].function.name
            log.info("poob.tool_route", tool=tool_name)
            return "", tool_name

        return choice.message.content or "", None

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
