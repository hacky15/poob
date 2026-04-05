"""Discord message handler — routes @mentions and DMs through PoobBrain."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord.ext import commands

from agentic_scraper.utils.logging import get_logger

if TYPE_CHECKING:
    from agentic_scraper.brain.poob import PoobBrain

log = get_logger("discord.agent")


class AgentMessageHandler(commands.Cog):
    """Listens for @mentions and DMs, routes them through PoobBrain.

    PoobBrain handles personality and routes deal-related requests
    to the deal sub-agent automatically.

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
            content = "hi"

        user_id = str(message.author.id)
        channel_id = str(message.channel.id)

        log.info(
            "agent.message_received",
            user=str(message.author),
            channel=channel_id,
            is_dm=is_dm,
            content_length=len(content),
        )

        log.info("agent.calling_brain", user=user_id, content=content[:80])
        try:
            async with message.channel.typing():
                response = await self._brain.respond(
                    content, user_id, channel_id, voice=False,
                )
            log.info("agent.brain_done", user=user_id, response_length=len(response))
        except Exception:
            log.exception("agent.run_error", user=user_id)
            response = "Sorry, something went wrong processing your request."

        # Discord has a 2000-char limit; split if needed
        try:
            for chunk in _split_message(response):
                await message.reply(chunk, mention_author=False)
            log.info("agent.reply_sent", user=user_id)
        except Exception:
            log.exception("agent.reply_error", user=user_id)


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
