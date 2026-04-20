"""DealNotifier - formats and sends deal alerts to Discord channels."""

from __future__ import annotations

from typing import TYPE_CHECKING

from poob.discord_bot.formatter import format_deal_embed
from poob.utils.logging import get_logger

if TYPE_CHECKING:
    import discord

    from poob.storage.models import Deal, Listing

log = get_logger("discord.notifier")

# Reaction emoji → feedback type mapping
FEEDBACK_REACTIONS: dict[str, str] = {
    "\u2705": "claimed",        # ✅
    "\u274c": "overpriced",     # ❌
    "\U0001f6ab": "scam",       # 🚫
    "\U0001f610": "not_interested",  # 😐
}


async def _add_feedback_reactions(message: discord.Message) -> None:
    """Add feedback reaction buttons to a deal notification message."""
    for emoji in FEEDBACK_REACTIONS:
        try:
            await message.add_reaction(emoji)
        except Exception:
            pass  # Don't fail the notification if a reaction can't be added


class DealNotifier:
    """Sends formatted deal alerts to Discord channels and user DMs.

    Tracks message→deal mappings so the bot can collect reaction-based feedback.

    Args:
        default_channel: The default Discord channel for deal notifications.
    """

    def __init__(self, default_channel: discord.TextChannel | None = None) -> None:
        self._default_channel = default_channel
        self._bot: discord.Client | None = None
        self._deal_messages: dict[int, str] = {}  # message_id → deal_id

    def set_channel(self, channel: discord.TextChannel) -> None:
        """Set or update the default notification channel."""
        self._default_channel = channel

    def set_bot(self, bot: discord.Client) -> None:
        """Store bot reference for sending DMs."""
        self._bot = bot

    def get_deal_id_for_message(self, message_id: int) -> str | None:
        """Look up the deal ID associated with a notification message.

        Args:
            message_id: The Discord message ID.

        Returns:
            The deal ID, or None if this message isn't a tracked notification.
        """
        return self._deal_messages.get(message_id)

    async def send_deal(
        self,
        deal: Deal,
        listing: Listing,
        channel: discord.TextChannel | None = None,
    ) -> None:
        """Format and send a deal alert embed.

        Args:
            deal: The deal to notify about.
            listing: The associated listing.
            channel: Target channel (uses default if not provided).
        """
        target = channel or self._default_channel
        if target is None:
            log.warning("No channel available for deal notification", deal_id=deal.id)
            return

        embed = format_deal_embed(deal, listing)
        try:
            msg = await target.send(embed=embed)
            await _add_feedback_reactions(msg)
            if deal.id:
                self._deal_messages[msg.id] = deal.id
            log.info("Deal notification sent", deal_id=deal.id, channel=target.name)
        except Exception as exc:
            log.error("Failed to send deal notification", deal_id=deal.id, error=str(exc))

    async def send_batch(
        self,
        deals_with_listings: list[tuple[Deal, Listing]],
        channel: discord.TextChannel | None = None,
    ) -> None:
        """Send multiple deal alerts.

        Args:
            deals_with_listings: List of (Deal, Listing) pairs.
            channel: Target channel (uses default if not provided).
        """
        for deal, listing in deals_with_listings:
            await self.send_deal(deal, listing, channel)

    async def send_deal_dm(
        self,
        deal: Deal,
        listing: Listing,
        discord_user_id: int,
        watch_interest: str = "",
    ) -> None:
        """Send a deal alert as a DM to a specific Discord user.

        Args:
            deal: The deal to notify about.
            listing: The associated listing.
            discord_user_id: Discord user ID to DM.
            watch_interest: The watchlist interest that matched.
        """
        if not self._bot:
            log.warning("No bot reference for DM", user_id=discord_user_id)
            return

        try:
            user = await self._bot.fetch_user(discord_user_id)
            embed = format_deal_embed(deal, listing, watch_interest=watch_interest)
            msg = await user.send(embed=embed)
            await _add_feedback_reactions(msg)
            if deal.id:
                self._deal_messages[msg.id] = deal.id
            log.info(
                "Deal DM sent",
                user_id=discord_user_id,
                deal_id=deal.id,
                interest=watch_interest,
            )
        except Exception as exc:
            log.error(
                "Failed to send deal DM",
                user_id=discord_user_id,
                error=str(exc),
            )
