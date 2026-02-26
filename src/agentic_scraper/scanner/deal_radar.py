"""DealRadar - autonomous LLM-powered deal scoring."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

from langchain_core.messages import HumanMessage

from agentic_scraper.storage.models import Deal, DealScore, Listing
from agentic_scraper.utils.logging import get_logger

if TYPE_CHECKING:
    from langchain_core.language_models.chat_models import BaseChatModel

log = get_logger("scanner.deal_radar")

# Score ranking for comparison
_SCORE_RANK: dict[DealScore, int] = {
    DealScore.UNKNOWN: 0,
    DealScore.FAIR: 1,
    DealScore.GOOD: 2,
    DealScore.GREAT: 3,
    DealScore.INCREDIBLE: 4,
}

EVALUATION_PROMPT = """Analyze this marketplace listing and determine if it's priced below market value.

Title: {title}
Price: ${price}
Description: {description}

Respond in JSON only, no additional text:
{{
    "estimated_market_price": <float>,
    "deal_score": "fair" | "good" | "great" | "incredible",
    "reasoning": "<1-2 sentences explaining why>"
}}"""


class DealRadar:
    """Autonomous deal detection using LLM evaluation.

    Evaluates listings against market prices without requiring a watch item.
    This is the "slow, smart" pass that complements the fast WatchlistMatcher.

    Args:
        min_score: Minimum DealScore to include in results.
    """

    def __init__(self, min_score: DealScore = DealScore.GOOD) -> None:
        self._min_score = min_score

    async def evaluate(
        self, listings: list[Listing], llm: BaseChatModel
    ) -> list[Deal]:
        """Evaluate listings using the LLM to detect deals.

        Args:
            listings: Listings to evaluate.
            llm: LangChain chat model for evaluation.

        Returns:
            List of Deal objects for listings scoring at or above min_score.
        """
        deals: list[Deal] = []

        for listing in listings:
            deal = await self._evaluate_single(listing, llm)
            if deal is not None:
                deals.append(deal)

        log.info(
            "Deal radar evaluation complete",
            evaluated=len(listings),
            deals_found=len(deals),
        )
        return deals

    async def _evaluate_single(
        self, listing: Listing, llm: BaseChatModel
    ) -> Deal | None:
        """Evaluate a single listing.

        Returns a Deal if the listing meets the min_score threshold, None otherwise.
        """
        prompt = EVALUATION_PROMPT.format(
            title=listing.title,
            price=listing.price or 0,
            description=listing.description or "No description",
        )

        try:
            response = await llm.ainvoke([HumanMessage(content=prompt)])
            result = self._parse_response(response.content)
        except Exception as exc:
            log.warning(
                "DealRadar evaluation failed",
                listing_id=listing.id,
                error=str(exc),
            )
            return None

        if result is None:
            return None

        score = result["score"]
        if _SCORE_RANK.get(score, 0) < _SCORE_RANK.get(self._min_score, 0):
            return None

        discount_pct = 0.0
        if result["market_price"] and listing.price and result["market_price"] > 0:
            discount_pct = (
                (result["market_price"] - listing.price) / result["market_price"]
            ) * 100.0

        return Deal(
            listing_id=listing.id or "",
            score=score,
            estimated_market_price=result["market_price"],
            discount_pct=max(discount_pct, 0.0),
            llm_reasoning=result["reasoning"],
        )

    @staticmethod
    def _parse_response(content: str) -> dict | None:
        """Parse the LLM response JSON.

        Handles raw JSON, markdown-fenced JSON, and returns None for invalid responses.
        """
        if not content or not content.strip():
            return None

        # Try extracting from markdown code fence
        fence_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?\s*```", content, re.DOTALL)
        json_str = fence_match.group(1).strip() if fence_match else content.strip()

        try:
            data = json.loads(json_str)
        except json.JSONDecodeError:
            log.warning("DealRadar: failed to parse LLM JSON", content_preview=content[:200])
            return None

        if not isinstance(data, dict):
            return None

        try:
            score_str = data.get("deal_score", "unknown")
            score = DealScore(score_str)
        except ValueError:
            score = DealScore.UNKNOWN

        return {
            "market_price": float(data.get("estimated_market_price", 0)),
            "score": score,
            "reasoning": str(data.get("reasoning", "")),
        }
