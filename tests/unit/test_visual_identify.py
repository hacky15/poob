"""Tests for VisualIdentifyTool - image-based item identification."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from poob.skills.models import ItemIdentification


def _make_llm_response(data: dict) -> MagicMock:
    resp = MagicMock()
    resp.content = json.dumps(data)
    return resp


class TestVisualIdentifyTool:
    """Tests for the visual identification skill."""

    async def test_identifies_item_from_image_url(self):
        """Should identify an item when given image URLs."""
        from poob.skills.visual import VisualIdentifyTool

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

        with patch("poob.skills.visual.httpx.AsyncClient") as mock_client_cls:
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
        from poob.skills.visual import VisualIdentifyTool

        llm = MagicMock()

        with patch("poob.skills.visual.httpx.AsyncClient") as mock_client_cls:
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
        from poob.skills.visual import VisualIdentifyTool

        llm = MagicMock()
        tool = VisualIdentifyTool(llm)
        result = await tool.run(image_urls=[])

        assert result.confidence == 0.0
        assert result.needs_visual is True

    async def test_handles_llm_error(self):
        """Should return low-confidence result when vision LLM fails."""
        from poob.skills.visual import VisualIdentifyTool

        llm = MagicMock()
        llm.ainvoke = AsyncMock(side_effect=RuntimeError("Vision model not available"))

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.content = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100

        with patch("poob.skills.visual.httpx.AsyncClient") as mock_client_cls:
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
        from poob.skills.visual import VisualIdentifyTool

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

        with patch("poob.skills.visual.httpx.AsyncClient") as mock_client_cls:
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

    async def test_multi_image_sends_multiple_to_llm(self):
        """Should send multiple images in a single LLM call."""
        from poob.skills.visual import VisualIdentifyTool

        llm = MagicMock()
        llm.ainvoke = AsyncMock(return_value=_make_llm_response({
            "item_name": "Sony PlayStation 5",
            "brand": "Sony",
            "category": "electronics/gaming",
            "confidence": 0.95,
            "needs_visual": False,
        }))

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.content = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100

        with patch("poob.skills.visual.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            tool = VisualIdentifyTool(llm, max_images=3)
            result = await tool.run(image_urls=[
                "https://example.com/img1.jpg",
                "https://example.com/img2.jpg",
                "https://example.com/img3.jpg",
            ])

        assert result.confidence > 0.5
        # Verify the LLM received multiple image_url entries
        call_args = llm.ainvoke.call_args[0][0]  # messages list
        message_content = call_args[0].content
        image_parts = [p for p in message_content if p.get("type") == "image_url"]
        assert len(image_parts) == 3

    async def test_multi_image_respects_max_images(self):
        """Should limit images sent to LLM based on max_images config."""
        from poob.skills.visual import VisualIdentifyTool

        llm = MagicMock()
        llm.ainvoke = AsyncMock(return_value=_make_llm_response({
            "item_name": "Table",
            "confidence": 0.7,
            "needs_visual": False,
        }))

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.content = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100

        with patch("poob.skills.visual.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            tool = VisualIdentifyTool(llm, max_images=2)
            await tool.run(image_urls=[
                "https://example.com/img1.jpg",
                "https://example.com/img2.jpg",
                "https://example.com/img3.jpg",
                "https://example.com/img4.jpg",
            ])

        call_args = llm.ainvoke.call_args[0][0]
        message_content = call_args[0].content
        image_parts = [p for p in message_content if p.get("type") == "image_url"]
        assert len(image_parts) == 2

    async def test_multi_image_partial_fetch_failure(self):
        """Should send only successfully fetched images to LLM."""
        from poob.skills.visual import VisualIdentifyTool

        llm = MagicMock()
        llm.ainvoke = AsyncMock(return_value=_make_llm_response({
            "item_name": "Chair",
            "confidence": 0.6,
            "needs_visual": False,
        }))

        mock_success = MagicMock()
        mock_success.status_code = 200
        mock_success.content = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100

        with patch("poob.skills.visual.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(side_effect=[
                mock_success,
                Exception("Timeout"),
                mock_success,
            ])
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            tool = VisualIdentifyTool(llm, max_images=3)
            result = await tool.run(image_urls=[
                "https://example.com/img1.jpg",
                "https://example.com/img2.jpg",
                "https://example.com/img3.jpg",
            ])

        assert result.confidence > 0.0
        call_args = llm.ainvoke.call_args[0][0]
        message_content = call_args[0].content
        image_parts = [p for p in message_content if p.get("type") == "image_url"]
        assert len(image_parts) == 2  # Only 2 fetched successfully

    async def test_parses_urgency_signals(self):
        """Should extract urgency signals from visual LLM response."""
        from poob.skills.visual import VisualIdentifyTool

        llm = MagicMock()
        llm.ainvoke = AsyncMock(return_value=_make_llm_response({
            "item_name": "Leather Couch",
            "brand": None,
            "category": "furniture/sofa",
            "condition": "good",
            "confidence": 0.8,
            "needs_visual": False,
            "urgency_signals": ["curb alert", "free"],
        }))

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.content = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100

        with patch("poob.skills.visual.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            tool = VisualIdentifyTool(llm)
            result = await tool.run(
                image_urls=["https://example.com/couch.jpg"],
                title="FREE couch curb alert",
                description="Come get it",
            )

        assert isinstance(result.urgency_signals, tuple)
        assert "curb alert" in result.urgency_signals
        assert "free" in result.urgency_signals

    async def test_passes_title_and_description_to_prompt(self):
        """Should include title and description in the visual LLM prompt."""
        from poob.skills.visual import VisualIdentifyTool

        llm = MagicMock()
        llm.ainvoke = AsyncMock(return_value=_make_llm_response({
            "item_name": "Chair",
            "confidence": 0.7,
            "needs_visual": False,
            "urgency_signals": [],
        }))

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.content = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100

        with patch("poob.skills.visual.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            tool = VisualIdentifyTool(llm)
            await tool.run(
                image_urls=["https://example.com/img.jpg"],
                title="Must sell this chair",
                description="Moving next week",
            )

        call_args = llm.ainvoke.call_args[0][0]
        message_content = call_args[0].content
        text_part = next(p for p in message_content if p.get("type") == "text")
        assert "Must sell this chair" in text_part["text"]
        assert "Moving next week" in text_part["text"]
