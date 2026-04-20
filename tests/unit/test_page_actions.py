"""Unit tests for browser page action utilities."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from poob.browser.page_actions import (
    evaluate_js,
    extract_page_text,
    navigate_and_wait,
    scroll_page,
    take_screenshot,
)


@pytest.fixture
def mock_page():
    """Mock CDP Page with standard methods."""
    page = AsyncMock()
    page.goto = AsyncMock()
    page.evaluate = AsyncMock(return_value="")
    page.screenshot = AsyncMock(return_value="base64data")
    page._extract_clean_markdown = AsyncMock(return_value=("# Page content", {}))
    return page


class TestNavigateAndWait:
    """Tests for navigate_and_wait()."""

    async def test_calls_goto_with_url(self, mock_page):
        """Should call page.goto() with the provided URL."""
        await navigate_and_wait(mock_page, "https://example.com")
        mock_page.goto.assert_called_once_with("https://example.com")

    async def test_applies_delay_after_navigation(self, mock_page):
        """Should pause after navigation to let content load."""
        with patch(
            "poob.browser.page_actions.random_delay", new_callable=AsyncMock
        ) as mock_delay:
            await navigate_and_wait(mock_page, "https://example.com", wait_ms=2000)
            mock_delay.assert_called_once()
            args = mock_delay.call_args[0]
            # Should use the provided wait_ms as the minimum
            assert args[0] == 2000

    async def test_default_wait_ms(self, mock_page):
        """Should use 3000ms as default wait time."""
        with patch(
            "poob.browser.page_actions.random_delay", new_callable=AsyncMock
        ) as mock_delay:
            await navigate_and_wait(mock_page, "https://example.com")
            args = mock_delay.call_args[0]
            assert args[0] == 3000


class TestScrollPage:
    """Tests for scroll_page()."""

    async def test_executes_scroll_steps(self, mock_page):
        """Should call page.evaluate() for each scroll step."""
        pattern = [("down", 300, 500), ("up", 100, 300)]
        with patch(
            "poob.browser.page_actions.random_delay", new_callable=AsyncMock
        ):
            await scroll_page(mock_page, pattern)
        assert mock_page.evaluate.call_count == 2

    async def test_scroll_down_positive_pixels(self, mock_page):
        """Scrolling down should use positive pixel value."""
        pattern = [("down", 400, 500)]
        with patch(
            "poob.browser.page_actions.random_delay", new_callable=AsyncMock
        ):
            await scroll_page(mock_page, pattern)
        js_call = mock_page.evaluate.call_args[0][0]
        assert "400" in js_call

    async def test_scroll_up_negative_pixels(self, mock_page):
        """Scrolling up should use negative pixel value."""
        pattern = [("up", 200, 500)]
        with patch(
            "poob.browser.page_actions.random_delay", new_callable=AsyncMock
        ):
            await scroll_page(mock_page, pattern)
        js_call = mock_page.evaluate.call_args[0][0]
        assert "-200" in js_call

    async def test_pauses_between_steps(self, mock_page):
        """Should delay between each scroll step."""
        pattern = [("down", 300, 500), ("down", 200, 400)]
        with patch(
            "poob.browser.page_actions.random_delay", new_callable=AsyncMock
        ) as mock_delay:
            await scroll_page(mock_page, pattern)
        assert mock_delay.call_count == 2


class TestExtractPageText:
    """Tests for extract_page_text()."""

    async def test_returns_markdown(self, mock_page):
        """Should return the markdown content from the page."""
        mock_page._extract_clean_markdown = AsyncMock(
            return_value=("# Title\nSome content", {"chars": 20})
        )
        result = await extract_page_text(mock_page)
        assert result == "# Title\nSome content"

    async def test_delegates_to_extract_clean_markdown(self, mock_page):
        """Should call page._extract_clean_markdown()."""
        await extract_page_text(mock_page)
        mock_page._extract_clean_markdown.assert_called_once()


class TestEvaluateJs:
    """Tests for evaluate_js()."""

    async def test_delegates_to_page_evaluate(self, mock_page):
        """Should call page.evaluate() with the script."""
        mock_page.evaluate = AsyncMock(return_value='[{"title": "Test"}]')
        result = await evaluate_js(mock_page, "() => document.title")
        mock_page.evaluate.assert_called_once_with("() => document.title")
        assert result == '[{"title": "Test"}]'


class TestTakeScreenshot:
    """Tests for take_screenshot()."""

    async def test_returns_base64(self, mock_page):
        """Should return base64 screenshot data."""
        mock_page.screenshot = AsyncMock(return_value="iVBORw0KGgo=")
        result = await take_screenshot(mock_page)
        assert result == "iVBORw0KGgo="
        mock_page.screenshot.assert_called_once()
