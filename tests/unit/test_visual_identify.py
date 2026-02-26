"""Tests for VisualIdentifyTool - image-based item identification."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agentic_scraper.skills.models import ItemIdentification


def _make_llm_response(data: dict) -> MagicMock:
    resp = MagicMock()
    resp.content = json.dumps(data)
    return resp


class TestVisualIdentifyTool:
    """Tests for the visual identification skill."""

    async def test_identifies_item_from_image_url(self):
        """Should identify an item when given image URLs."""
        from agentic_scraper.skills.visual import VisualIdentifyTool

        llm = MagicMock()
        llm.ainvoke = AsyncMock(return_value=_make_llm_response({
            "item_name": "West Elm Mid-Century Dining Table",
            "brand": "West Elm",
            "model": "Mid-Century Expandable Dining Table",
            "category": "furniture/table/dining",
            "condition": "good",
            "confidence": 0.85,
            "needs_visual": False,
        }))

        # Mock httpx to return fake image bytes
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.content = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100  # Fake PNG

        with patch("agentic_scraper.skills.visual.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            tool = VisualIdentifyTool(llm)
            result = await tool.run(image_urls=["https://example.com/table.jpg"])

        assert isinstance(result, ItemIdentification)
        assert result.confidence > 0.5
        assert "table" in result.item_name.lower() or "West Elm" in result.item_name

    async def test_handles_image_download_failure(self):
        """Should return low-confidence result when image can't be fetched."""
        from agentic_scraper.skills.visual import VisualIdentifyTool

        llm = MagicMock()

        with patch("agentic_scraper.skills.visual.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(side_effect=Exception("Connection failed"))
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            tool = VisualIdentifyTool(llm)
            result = await tool.run(image_urls=["https://example.com/bad.jpg"])

        assert result.confidence == 0.0
        assert result.needs_visual is True

    async def test_handles_empty_image_list(self):
        """Should return empty result when no images provided."""
        from agentic_scraper.skills.visual import VisualIdentifyTool

        llm = MagicMock()
        tool = VisualIdentifyTool(llm)
        result = await tool.run(image_urls=[])

        assert result.confidence == 0.0
        assert result.needs_visual is True

    async def test_handles_llm_error(self):
        """Should return low-confidence result when vision LLM fails."""
        from agentic_scraper.skills.visual import VisualIdentifyTool

        llm = MagicMock()
        llm.ainvoke = AsyncMock(side_effect=RuntimeError("Vision model not available"))

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.content = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100

        with patch("agentic_scraper.skills.visual.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            tool = VisualIdentifyTool(llm)
            result = await tool.run(image_urls=["https://example.com/table.jpg"])

        assert result.confidence == 0.0

    async def test_uses_first_successful_image(self):
        """Should try multiple images and use the first successful one."""
        from agentic_scraper.skills.visual import VisualIdentifyTool

        llm = MagicMock()
        llm.ainvoke = AsyncMock(return_value=_make_llm_response({
            "item_name": "Dining Table",
            "brand": None,
            "model": None,
            "category": "furniture/table",
            "condition": "good",
            "confidence": 0.7,
            "needs_visual": False,
        }))

        mock_fail = MagicMock()
        mock_fail.status_code = 404
        mock_fail.content = b""

        mock_success = MagicMock()
        mock_success.status_code = 200
        mock_success.content = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100

        with patch("agentic_scraper.skills.visual.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(side_effect=[
                Exception("404"),
                mock_success,
            ])
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            tool = VisualIdentifyTool(llm)
            result = await tool.run(image_urls=[
                "https://example.com/bad.jpg",
                "https://example.com/good.jpg",
            ])

        assert result.confidence > 0.0
        assert llm.ainvoke.called
