"""VisualIdentifyTool - image-based item identification."""

from __future__ import annotations

import base64
import json
import re
from typing import TYPE_CHECKING

import httpx
from langchain_core.messages import HumanMessage

from poob.skills.llm_call import llm_call
from poob.skills.models import ItemIdentification, clean_optional
from poob.skills.prompts import VISUAL_IDENTIFY_PROMPT
from poob.utils.logging import get_logger

if TYPE_CHECKING:
    from langchain_core.language_models.chat_models import BaseChatModel

log = get_logger("skills.visual")

# Max images to try downloading
MAX_IMAGES_TO_TRY = 3


class VisualIdentifyTool:
    """Identify an item from listing photos using a vision-capable LLM.

    Args:
        llm: Vision-capable LangChain chat model.
        max_images: Maximum images to include in a single LLM call.
    """

    def __init__(self, llm: BaseChatModel, *, max_images: int = 3) -> None:
        self._llm = llm
        self._max_images = max_images

    async def run(
        self,
        image_urls: list[str] | None = None,
        screenshot_b64: str | None = None,
        title: str = "",
        description: str = "",
    ) -> ItemIdentification:
        """Identify an item from images.

        Sends up to max_images in a single LLM call for better identification.

        Args:
            image_urls: List of image URLs to try.
            screenshot_b64: Pre-encoded base64 screenshot (alternative to URLs).
            title: Listing title for context.
            description: Listing description for context.

        Returns:
            ItemIdentification with visual analysis results.
        """
        if not image_urls and not screenshot_b64:
            return ItemIdentification(confidence=0.0, needs_visual=True)

        # Get image data — fetch multiple images for better identification
        images_b64: list[str] = []
        if screenshot_b64:
            images_b64.append(screenshot_b64)
        if image_urls:
            fetched = await self._fetch_images(image_urls)
            images_b64.extend(fetched)

        if not images_b64:
            return ItemIdentification(confidence=0.0, needs_visual=True)

        # Send to vision LLM with all available images
        try:
            prompt_text = VISUAL_IDENTIFY_PROMPT.format(
                title=title or "Unknown",
                description=description or "No description",
            )
            content: list[dict] = [{"type": "text", "text": prompt_text}]
            for img_b64 in images_b64[: self._max_images]:
                content.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"},
                })
            message = HumanMessage(content=content)
            response = await llm_call(
                self._llm, [message], skill="visual_identify",
            )
            return self._parse_response(response.content)
        except Exception as exc:
            log.warning("Visual identification failed", error=str(exc))
            return ItemIdentification(confidence=0.0, needs_visual=True)

    async def _fetch_images(self, urls: list[str]) -> list[str]:
        """Fetch and encode multiple images from URLs.

        Args:
            urls: Image URLs to try downloading.

        Returns:
            List of base64-encoded image strings (may be fewer than requested).
        """
        results: list[str] = []
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            for url in urls[: self._max_images]:
                try:
                    response = await client.get(url)
                    if response.status_code == 200 and len(response.content) > 100:
                        results.append(
                            base64.b64encode(response.content).decode("utf-8")
                        )
                except Exception as exc:
                    log.debug("Failed to fetch image", url=url, error=str(exc))
                    continue
        return results

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
