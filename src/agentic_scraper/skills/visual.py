"""VisualIdentifyTool - image-based item identification."""

from __future__ import annotations

import base64
import json
import re
from typing import TYPE_CHECKING

import httpx
from langchain_core.messages import HumanMessage

from agentic_scraper.skills.models import ItemIdentification
from agentic_scraper.skills.prompts import VISUAL_IDENTIFY_PROMPT
from agentic_scraper.utils.logging import get_logger

if TYPE_CHECKING:
    from langchain_core.language_models.chat_models import BaseChatModel

log = get_logger("skills.visual")

# Max images to try downloading
MAX_IMAGES_TO_TRY = 3


class VisualIdentifyTool:
    """Identify an item from listing photos using a vision-capable LLM.

    Args:
        llm: Vision-capable LangChain chat model.
    """

    def __init__(self, llm: BaseChatModel) -> None:
        self._llm = llm

    async def run(
        self,
        image_urls: list[str] | None = None,
        screenshot_b64: str | None = None,
    ) -> ItemIdentification:
        """Identify an item from images.

        Args:
            image_urls: List of image URLs to try.
            screenshot_b64: Pre-encoded base64 screenshot (alternative to URLs).

        Returns:
            ItemIdentification with visual analysis results.
        """
        if not image_urls and not screenshot_b64:
            return ItemIdentification(confidence=0.0, needs_visual=True)

        # Get image data
        image_b64 = screenshot_b64
        if not image_b64 and image_urls:
            image_b64 = await self._fetch_first_image(image_urls)

        if not image_b64:
            return ItemIdentification(confidence=0.0, needs_visual=True)

        # Send to vision LLM
        try:
            message = HumanMessage(
                content=[
                    {"type": "text", "text": VISUAL_IDENTIFY_PROMPT},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"},
                    },
                ]
            )
            response = await self._llm.ainvoke([message])
            return self._parse_response(response.content)
        except Exception as exc:
            log.warning("Visual identification failed", error=str(exc))
            return ItemIdentification(confidence=0.0, needs_visual=True)

    async def _fetch_first_image(self, urls: list[str]) -> str | None:
        """Try to fetch and encode the first successful image from URLs."""
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            for url in urls[:MAX_IMAGES_TO_TRY]:
                try:
                    response = await client.get(url)
                    if response.status_code == 200 and len(response.content) > 100:
                        return base64.b64encode(response.content).decode("utf-8")
                except Exception as exc:
                    log.debug("Failed to fetch image", url=url, error=str(exc))
                    continue

        return None

    @staticmethod
    def _parse_response(content: str) -> ItemIdentification:
        """Parse vision LLM JSON response into ItemIdentification."""
        if not content or not content.strip():
            return ItemIdentification(confidence=0.0, needs_visual=True)

        fence_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?\s*```", content, re.DOTALL)
        json_str = fence_match.group(1).strip() if fence_match else content.strip()

        try:
            data = json.loads(json_str)
        except json.JSONDecodeError:
            log.warning("Failed to parse visual ID response", content_preview=content[:200])
            return ItemIdentification(confidence=0.0, needs_visual=True)

        if not isinstance(data, dict):
            return ItemIdentification(confidence=0.0, needs_visual=True)

        return ItemIdentification(
            item_name=str(data.get("item_name", "")),
            brand=data.get("brand"),
            model=data.get("model"),
            category=str(data.get("category", "")),
            condition=data.get("condition"),
            confidence=float(data.get("confidence", 0.0)),
            needs_visual=bool(data.get("needs_visual", False)),
        )
