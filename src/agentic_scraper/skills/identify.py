"""IdentifyItemTool - text-based item identification from listing title/description."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

from langchain_core.messages import HumanMessage

from agentic_scraper.skills.llm_call import llm_call
from agentic_scraper.skills.models import ItemIdentification, clean_optional
from agentic_scraper.skills.prompts import IDENTIFY_ITEM_PROMPT
from agentic_scraper.utils.logging import get_logger

if TYPE_CHECKING:
    from langchain_core.language_models.chat_models import BaseChatModel

log = get_logger("skills.identify")


class IdentifyItemTool:
    """Identify an item from listing text using LLM analysis.

    Args:
        llm: LangChain chat model for text analysis.
    """

    def __init__(self, llm: BaseChatModel) -> None:
        self._llm = llm

    async def run(self, title: str, description: str = "") -> ItemIdentification:
        """Extract structured item details from listing text.

        Args:
            title: Listing title.
            description: Listing description (may be empty).

        Returns:
            ItemIdentification with extracted details and confidence score.
        """
        prompt = IDENTIFY_ITEM_PROMPT.format(
            title=title,
            description=description or "No description provided",
        )

        try:
            response = await llm_call(
                self._llm, [HumanMessage(content=prompt)], skill="identify",
            )
            return self._parse_response(response.content)
        except Exception as exc:
            log.warning("Item identification failed", error=str(exc), title=title)
            return ItemIdentification(
                item_name=title,
                confidence=0.0,
                needs_visual=True,
            )

    @staticmethod
    def _parse_response(content: str) -> ItemIdentification:
        """Parse LLM JSON response into ItemIdentification."""
        if not content or not content.strip():
            return ItemIdentification(confidence=0.0, needs_visual=True)

        # Extract JSON from markdown fence if present
        fence_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?\s*```", content, re.DOTALL)
        json_str = fence_match.group(1).strip() if fence_match else content.strip()

        try:
            data = json.loads(json_str)
        except json.JSONDecodeError:
            log.warning("Failed to parse identify response", content_preview=content[:200])
            return ItemIdentification(confidence=0.0, needs_visual=True)

        if not isinstance(data, dict):
            return ItemIdentification(confidence=0.0, needs_visual=True)

        raw_signals = data.get("urgency_signals", [])
        if isinstance(raw_signals, list):
            signals = tuple(str(s) for s in raw_signals if s)
        else:
            signals = ()

        return ItemIdentification(
            item_name=str(data.get("item_name", "")),
            brand=clean_optional(data.get("brand")),
            model=clean_optional(data.get("model")),
            category=str(data.get("category", "")),
            condition=clean_optional(data.get("condition")),
            confidence=float(data.get("confidence", 0.0)),
            needs_visual=bool(data.get("needs_visual", False)),
            urgency_signals=signals,
        )
