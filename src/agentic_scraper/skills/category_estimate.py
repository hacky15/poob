"""CategoryEstimateTool - category-level pricing fallback."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

from langchain_core.messages import HumanMessage

from agentic_scraper.skills.models import CategoryEstimate
from agentic_scraper.skills.prompts import CATEGORY_ESTIMATE_PROMPT
from agentic_scraper.utils.logging import get_logger

if TYPE_CHECKING:
    from langchain_core.language_models.chat_models import BaseChatModel

log = get_logger("skills.category_estimate")

# Category estimates should never exceed this confidence
MAX_CATEGORY_CONFIDENCE = 0.6


class CategoryEstimateTool:
    """Estimate a price range based on item category.

    This is the lowest-confidence skill - used only when eBay and retail
    lookups fail. Still better than a pure LLM guess because it's
    category-anchored with depreciation heuristics.

    Args:
        llm: LangChain chat model for estimation.
    """

    def __init__(self, llm: BaseChatModel) -> None:
        self._llm = llm

    async def run(
        self,
        category: str,
        condition: str | None = None,
        description_hints: str = "",
    ) -> CategoryEstimate:
        """Estimate price range for an item category.

        Args:
            category: Hierarchical category (e.g. "furniture/table").
            condition: Item condition (new, like new, good, fair, parts).
            description_hints: Additional text to help narrow the estimate.

        Returns:
            CategoryEstimate with low/high/typical price range.
        """
        prompt = CATEGORY_ESTIMATE_PROMPT.format(
            category=category,
            condition=condition or "unknown",
            description_hints=description_hints or "none",
        )

        try:
            response = await self._llm.ainvoke([HumanMessage(content=prompt)])
            return self._parse_response(response.content)
        except Exception as exc:
            log.warning(
                "Category estimate failed",
                error=str(exc),
                category=category,
            )
            return CategoryEstimate(confidence=0.0)

    @staticmethod
    def _parse_response(content: str) -> CategoryEstimate:
        """Parse LLM JSON response into CategoryEstimate."""
        if not content or not content.strip():
            return CategoryEstimate(confidence=0.0)

        fence_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?\s*```", content, re.DOTALL)
        json_str = fence_match.group(1).strip() if fence_match else content.strip()

        try:
            data = json.loads(json_str)
        except json.JSONDecodeError:
            log.warning("Failed to parse category estimate", content_preview=content[:200])
            return CategoryEstimate(confidence=0.0)

        if not isinstance(data, dict):
            return CategoryEstimate(confidence=0.0)

        # Cap confidence - category estimates are inherently lower confidence
        raw_confidence = float(data.get("confidence", 0.0))
        capped_confidence = min(raw_confidence, MAX_CATEGORY_CONFIDENCE)

        return CategoryEstimate(
            low_price=float(data.get("low_price", 0.0)),
            high_price=float(data.get("high_price", 0.0)),
            typical_price=float(data.get("typical_price", 0.0)),
            source="category_estimate",
            confidence=round(capped_confidence, 2),
        )
