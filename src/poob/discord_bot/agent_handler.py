"""Discord message handler — routes @mentions and DMs through PoobBrain.

Single source of truth for text messages targeting Poob. Flow:

1. Generate response via :class:`PoobBrain` (LLM tool calling may route
   to deal or music sub-agents — all signalled via structured tool_args
   inside the brain).
2. Post the text reply in the originating channel.
3. Ask :class:`VoiceCog` to speak the response in VC — but only if the
   author shares Poob's voice channel. Deterministic gate, no opt-in.
4. If the response was music-related and a track is now playing, ask
   :class:`MusicCog` for a (embed, persistent-view) pair and post it so
   users get the now-playing card with control buttons.

Fetches recent channel history so the brain has conversational context,
mirroring how voice sessions maintain a rolling transcript.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import discord
from discord.ext import commands

from poob.utils.logging import get_logger

if TYPE_CHECKING:
    from poob.brain.poob import PoobBrain

log = get_logger("discord.agent")

# How many recent messages to fetch from the channel for context.
_CONTEXT_WINDOW = 15

# Matches Discord mention syntax: <@123456> or <@!123456>
_MENTION_RE = re.compile(r"<@!?\d+>")


class AgentMessageHandler(commands.Cog):
    """Listens for @mentions and DMs, routes them through PoobBrain.

    PoobBrain handles personality and routes deal-related requests
    to the deal sub-agent automatically.

    Fetches the last few channel messages so the brain sees conversational
    context (like voice's rolling transcript), not just the single message
    that triggered it.

    Ignores messages with the command prefix (those are handled by existing cogs)
    and messages from other bots.

    Args:
        bot: The Discord bot instance.
        brain: The unified PoobBrain personality layer.
    """

    def __init__(self, bot: commands.Bot, brain: PoobBrain) -> None:
        self._bot = bot
        self._brain = brain

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """Handle incoming messages for the conversational agent."""
        # Ignore our own messages and other bots
        if message.author.bot:
            return

        # Ignore command-prefixed messages (let existing cogs handle them)
        if message.content.startswith(self._bot.command_prefix):
            return

        # Respond to DMs or @mentions
        is_dm = isinstance(message.channel, discord.DMChannel)
        is_mentioned = self._bot.user in message.mentions if self._bot.user else False

        if not is_dm and not is_mentioned:
            return

        # Strip the mention from the message content
        content = message.content
        if is_mentioned and self._bot.user:
            content = content.replace(f"<@{self._bot.user.id}>", "").strip()
            content = content.replace(f"<@!{self._bot.user.id}>", "").strip()

        if not content:
            content = ""  # Will be resolved with context below

        user_id = str(message.author.id)
        channel_id = str(message.channel.id)

        # Build channel context — recent messages the brain can reference.
        # For guild channels, fetch history; DMs already have per-user history
        # in PoobBrain so context is less critical (but still helpful).
        channel_context = await self._fetch_channel_context(message)

        # If the user sent only a mention (no text), the context IS the message.
        # The brain should infer intent from what was just said in the channel.
        if not content:
            if channel_context:
                content = "(responding to the conversation above)"
            else:
                content = "hi"

        # Prepend context using the same format voice sessions use, which the
        # system prompt already knows how to handle.
        if channel_context:
            brain_input = (
                f"[Recent conversation you've been listening to:\n"
                f"{channel_context}]\n\n"
                f"{message.author.display_name} said to you: {content}"
            )
        else:
            brain_input = content

        log.info(
            "agent.message_received",
            user=str(message.author),
            channel=channel_id,
            is_dm=is_dm,
            content_length=len(content),
            context_lines=channel_context.count("\n") + 1 if channel_context else 0,
        )

        guild_id = message.guild.id if message.guild else 0

        log.info("agent.calling_brain", user=user_id, content=content[:80])
        try:
            async with message.channel.typing():
                response = await self._brain.respond(
                    brain_input, user_id, channel_id, voice=False,
                    guild_id=guild_id,
                )
            log.info("agent.brain_done", user=user_id, response_length=len(response))
        except Exception:
            log.exception("agent.run_error", user=user_id)
            response = "Sorry, something went wrong processing your request."

        # Track whether music just started so we can attach the now-playing
        # card. We snapshot the current track *before* the brain call (which
        # may queue/play as a side effect) and compare after.
        music_cog = self._bot.get_cog("Music")
        track_before = _current_track(music_cog, message.guild.id if message.guild else 0)

        # Discord has a 2000-char limit; split if needed
        try:
            for chunk in _split_message(response):
                await message.reply(chunk, mention_author=False)
            log.info("agent.reply_sent", user=user_id)
        except Exception:
            log.exception("agent.reply_error", user=user_id)
            return

        # Speak in VC iff the author is in Poob's voice channel.
        # VoiceCog encapsulates the gate; we just ask.
        voice_cog = self._bot.get_cog("Voice")
        if voice_cog is not None and hasattr(voice_cog, "speak_if_in_channel"):
            try:
                await voice_cog.speak_if_in_channel(message, response)
            except Exception:
                log.exception("agent.voice_speak_error", user=user_id)

        # Post the now-playing card when a new track just started as a
        # side effect of this message (play/queue routed through the
        # brain's music_assistant tool).
        if music_cog is not None and message.guild is not None:
            track_after = _current_track(music_cog, message.guild.id)
            if track_after is not None and track_after is not track_before:
                try:
                    payload = music_cog.build_now_playing_message(message.guild.id)
                    if payload is not None:
                        embed, view = payload
                        await message.channel.send(embed=embed, view=view)
                        log.info("agent.now_playing_posted",
                                 track=track_after.title[:60])
                except Exception:
                    log.exception("agent.now_playing_error", user=user_id)

    # ------------------------------------------------------------------
    # Channel context
    # ------------------------------------------------------------------

    async def _fetch_channel_context(
        self, trigger_message: discord.Message,
    ) -> str:
        """Fetch recent channel messages as an attributed transcript.

        Returns a string like voice's _build_context():
            Ben: yeah thats what I was saying
            Natural: im gonna tame the beast
            Poob: oh you think you can?

        Only includes messages from the last few minutes of conversation.
        Skips the trigger message itself (that goes in the user's prompt).
        """
        try:
            lines: list[str] = []
            # history() returns newest-first; we collect then reverse.
            async for msg in trigger_message.channel.history(
                limit=_CONTEXT_WINDOW + 1,  # +1 because trigger msg is included
                before=None,
            ):
                if msg.id == trigger_message.id:
                    continue
                # Clean the message: strip mentions to readable names
                text = self._clean_message_content(msg)
                if not text:
                    continue
                name = msg.author.display_name
                lines.append(f"{name}: {text}")

            if not lines:
                return ""

            # Reverse so oldest is first (chronological order)
            lines.reverse()
            return "\n".join(lines)

        except (discord.Forbidden, discord.HTTPException) as exc:
            log.warning("channel_context.fetch_failed", error=str(exc)[:100])
            return ""

    def _clean_message_content(self, msg: discord.Message) -> str:
        """Clean a message for the context transcript.

        Replaces raw mention IDs with display names and strips embeds/attachments.
        """
        text = msg.content
        if not text:
            return ""

        # Replace <@ID> / <@!ID> mentions with display names
        for user in msg.mentions:
            text = text.replace(f"<@{user.id}>", f"@{user.display_name}")
            text = text.replace(f"<@!{user.id}>", f"@{user.display_name}")

        # Strip any remaining unresolved mentions
        text = _MENTION_RE.sub("", text).strip()
        return text


def _current_track(music_cog, guild_id: int):
    """Read the currently playing Track from MusicCog, if any.

    Used to detect "a new track started as a side effect of this message"
    by comparing identity before and after the brain call.
    """
    if music_cog is None or guild_id == 0:
        return None
    player = music_cog._get_player(guild_id)
    if player is None:
        return None
    return player.current_track


def _split_message(text: str, limit: int = 2000) -> list[str]:
    """Split a long message into chunks that fit Discord's character limit."""
    if len(text) <= limit:
        return [text]

    chunks = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        # Try to split at a newline
        split_at = text.rfind("\n", 0, limit)
        if split_at == -1:
            split_at = limit
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")

    return chunks
