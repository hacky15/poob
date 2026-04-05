"""Discord cog for collecting deal feedback via reactions."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord.ext import commands

from agentic_scraper.discord_bot.notifier import FEEDBACK_REACTIONS
from agentic_scraper.storage.models import DealFeedback
from agentic_scraper.utils.logging import get_logger

if TYPE_CHECKING:
    from agentic_scraper.discord_bot.notifier import DealNotifier
    from agentic_scraper.storage.repositories.deal_repo import DealRepository
    from agentic_scraper.storage.repositories.feedback_repo import FeedbackRepository
    from agentic_scraper.storage.repositories.listing_repo import ListingRepository

log = get_logger("cogs.feedback")


class FeedbackCog(commands.Cog, name="Feedback"):
    """Collects user feedback from reactions on deal notification embeds.

    Enriches feedback with full listing + deal context for the learning pipeline.
    """

    def __init__(
        self,
        bot: commands.Bot,
        notifier: DealNotifier,
        feedback_repo: FeedbackRepository,
        deal_repo: DealRepository | None = None,
        listing_repo: ListingRepository | None = None,
    ) -> None:
        self.bot = bot
        self._notifier = notifier
        self._feedback_repo = feedback_repo
        self._deal_repo = deal_repo
        self._listing_repo = listing_repo

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        """Record feedback when a user reacts to a deal notification."""
        # Ignore bot's own reactions
        if payload.user_id == self.bot.user.id:
            return

        emoji_str = str(payload.emoji)
        feedback_type = FEEDBACK_REACTIONS.get(emoji_str)
        if feedback_type is None:
            return  # Not a feedback reaction

        deal_id = self._notifier.get_deal_id_for_message(payload.message_id)
        if deal_id is None:
            return  # Not a tracked deal notification

        user_id_str = str(payload.user_id)

        # Prevent duplicate feedback from the same user on the same deal
        if await self._feedback_repo.exists(deal_id, user_id_str):
            return

        # Build enriched feedback with listing + deal context
        feedback = DealFeedback(
            deal_id=deal_id,
            discord_user_id=user_id_str,
            feedback_type=feedback_type,
        )

        # Enrich with deal + listing data for learning pipeline
        try:
            await self._enrich_feedback(feedback)
        except Exception as exc:
            log.debug("Feedback enrichment failed, saving basic", error=str(exc)[:100])

        await self._feedback_repo.save(feedback)
        log.info(
            "Deal feedback recorded",
            deal_id=deal_id,
            user_id=user_id_str,
            feedback_type=feedback_type,
            enriched=bool(feedback.listing_title),
        )

    async def _enrich_feedback(self, feedback: DealFeedback) -> None:
        """Populate enriched fields from the deal and listing data."""
        if not self._deal_repo or not self._listing_repo:
            return

        deal = await self._deal_repo.get(feedback.deal_id)
        if not deal:
            return

        listing = await self._listing_repo.get(deal.listing_id)
        if listing:
            feedback.listing_title = listing.title
            feedback.listing_price = listing.price or 0.0

        feedback.deal_quality = deal.score.value
        feedback.estimated_value = deal.estimated_market_price or 0.0
