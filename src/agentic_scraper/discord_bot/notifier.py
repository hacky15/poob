"""DealNotifier - formats and sends deal alerts to Discord channels."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentic_scraper.discord_bot.formatter import format_deal_embed
from agentic_scraper.utils.logging import get_logger

if TYPE_CHECKING:
    import discord

    from agentic_scraper.storage.models import Deal, Listing

log = get_logger("discord.notifier")


class DealNotifier:
    """Sends formatted deal alerts to Discord channels.

    Args:
        default_channel: The default Discord channel for deal notifications.
    """

    def __init__(self, default_channel: discord.TextChannel | None = None) -> None:
        self._default_channel = default_channel

    def set_channel(self, channel: discord.TextChannel) -> None:
        """Set or update the default notification channel."""
        self._default_channel = channel

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
            await target.send(embed=embed)
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
