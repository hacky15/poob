"""RetailLookupTool - MSRP/retail price lookup via web search."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

from langchain_core.messages import HumanMessage

from agentic_scraper.skills.llm_call import llm_call
from agentic_scraper.skills.models import PriceLookupResult
from agentic_scraper.skills.prompts import RETAIL_PRICE_EXTRACT_PROMPT
from agentic_scraper.utils.logging import get_logger

if TYPE_CHECKING:
    from langchain_core.language_models.chat_models import BaseChatModel

    from agentic_scraper.skills.web_search import SearchProvider

log = get_logger("skills.retail_lookup")


class RetailLookupTool:
    """Look up the retail/MSRP price of an item via Tavily web search.

    Args:
        llm: LangChain chat model for extracting prices from search results.
        search_provider: Tavily web search provider.
    """

    def __init__(
        self,
        llm: BaseChatModel,
        search_provider: SearchProvider,
    ) -> None:
        self._llm = llm
        self._search = search_provider

    async def run(
        self,
        item_name: str,
        brand: str | None = None,
        model: str | None = None,
    ) -> PriceLookupResult:
        """Search for the retail price of an item.

        Args:
            item_name: Name of the item.
            brand: Brand name (optional).
            model: Model number/name (optional).

        Returns:
            PriceLookupResult with retail price data.
        """
        # Build search query
        parts = []
        if brand:
            parts.append(brand)
        if model and model != item_name:
            parts.append(model)
        if not parts or item_name not in " ".join(parts):
            parts.append(item_name)
        parts.append("retail price MSRP")
        search_query = " ".join(parts)

        try:
            search_text = await self._search.search(search_query)

            if not search_text:
                log.warning("Retail search returned no results", query=search_query[:60])
                return self._empty_result(search_query)

            return await self._extract_price(
                search_text, item_name, brand, model, search_query
            )

        except Exception as exc:
            log.warning("Retail lookup failed", error=str(exc), item=item_name)
            return self._empty_result(search_query)

    async def _extract_price(
        self,
        search_text: str,
        item_name: str,
        brand: str | None,
        model: str | None,
        search_query: str,
    ) -> PriceLookupResult:
        """Use LLM to extract retail price from search result text."""
        prompt = RETAIL_PRICE_EXTRACT_PROMPT.format(
            item_name=item_name,
            brand=brand or "unknown",
            model=model or "unknown",
            search_text=search_text[:3000],  # Limit context size
        )

        try:
            response = await llm_call(
                self._llm, [HumanMessage(content=prompt)], skill="retail_extract",
            )
            return self._parse_llm_response(response.content, search_query)
        except Exception as exc:
            log.warning("LLM price extraction failed", error=str(exc))
            return self._empty_result(search_query)

    @staticmethod
    def _parse_llm_response(content: str, search_query: str) -> PriceLookupResult:
        """Parse LLM response to extract retail price."""
        fence_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?\s*```", content, re.DOTALL)
        json_str = fence_match.group(1).strip() if fence_match else content.strip()

        try:
            data = json.loads(json_str)
        except json.JSONDecodeError:
            return PriceLookupResult(
                sample_count=0, source="retail",
                search_query=search_query, confidence=0.0,
            )

        price = float(data.get("retail_price", 0))
        confidence = float(data.get("confidence", 0))

        if price <= 0:
            return PriceLookupResult(
                sample_count=0, source="retail",
                search_query=search_query, confidence=0.0,
            )

        return PriceLookupResult(
            median_price=price,
            average_price=price,
            min_price=price,
            max_price=price,
            sample_count=1,
            source="retail",
            search_query=search_query,
            confidence=round(confidence, 2),
        )

    @staticmethod
    def _empty_result(search_query: str) -> PriceLookupResult:
        return PriceLookupResult(
            sample_count=0, source="retail",
            search_query=search_query, confidence=0.0,
        )
