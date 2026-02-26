"""RetailLookupTool - MSRP/retail price lookup via web search."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING
from urllib.parse import quote_plus

import httpx
from langchain_core.messages import HumanMessage

from agentic_scraper.skills.models import PriceLookupResult
from agentic_scraper.skills.prompts import RETAIL_PRICE_EXTRACT_PROMPT
from agentic_scraper.utils.logging import get_logger

if TYPE_CHECKING:
    from langchain_core.language_models.chat_models import BaseChatModel

log = get_logger("skills.retail_lookup")

# Search URL for retail price lookups
SEARCH_URL = "https://html.duckduckgo.com/html/?q={query}"

SEARCH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
}


class RetailLookupTool:
    """Look up the retail/MSRP price of an item via web search.

    Args:
        llm: LangChain chat model for extracting prices from search results.
    """

    def __init__(self, llm: BaseChatModel) -> None:
        self._llm = llm

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

        url = SEARCH_URL.format(query=quote_plus(search_query))

        try:
            async with httpx.AsyncClient(
                headers=SEARCH_HEADERS,
                timeout=10,
                follow_redirects=True,
            ) as client:
                response = await client.get(url)

            if response.status_code != 200:
                log.warning("Retail search failed", status=response.status_code)
                return self._empty_result(search_query)

            search_text = self._extract_text(response.text)

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
            response = await self._llm.ainvoke([HumanMessage(content=prompt)])
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
    def _extract_text(html: str) -> str:
        """Extract visible text from HTML, stripping tags."""
        text = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL)
        text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text)
        return text.strip()

    @staticmethod
    def _empty_result(search_query: str) -> PriceLookupResult:
        return PriceLookupResult(
            sample_count=0, source="retail",
            search_query=search_query, confidence=0.0,
        )
