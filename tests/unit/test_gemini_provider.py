"""Tests for GeminiProvider."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


class TestGeminiProvider:
    """Tests for GeminiProvider initialization and protocol compliance."""

    @patch("agentic_scraper.llm.gemini_provider.ChatGoogleGenerativeAI")
    def test_creates_chat_model(self, mock_chat_cls):
        """GeminiProvider creates a ChatGoogleGenerativeAI instance."""
        from agentic_scraper.llm.gemini_provider import GeminiProvider

        provider = GeminiProvider(
            api_key="test-key", model="gemini-2.5-flash", temperature=0.3
        )
        mock_chat_cls.assert_called_once_with(
            model="gemini-2.5-flash",
            google_api_key="test-key",
            temperature=0.3,
        )
        assert provider.chat_model is mock_chat_cls.return_value

    @patch("agentic_scraper.llm.gemini_provider.ChatGoogleGenerativeAI")
    def test_model_name(self, mock_chat_cls):
        """model_name returns the configured model string."""
        from agentic_scraper.llm.gemini_provider import GeminiProvider

        provider = GeminiProvider(
            api_key="test-key", model="gemini-2.5-flash", temperature=0.3
        )
        assert provider.model_name == "gemini-2.5-flash"

    @patch("agentic_scraper.llm.gemini_provider.ChatGoogleGenerativeAI")
    @patch("agentic_scraper.llm.gemini_provider.httpx")
    def test_is_available_success(self, mock_httpx, mock_chat_cls):
        """is_available returns True when API responds with 200."""
        from agentic_scraper.llm.gemini_provider import GeminiProvider

        mock_httpx.get.return_value = MagicMock(status_code=200)
        provider = GeminiProvider(
            api_key="test-key", model="gemini-2.5-flash", temperature=0.3
        )
        assert provider.is_available() is True
        mock_httpx.get.assert_called_once()

    @patch("agentic_scraper.llm.gemini_provider.ChatGoogleGenerativeAI")
    @patch("agentic_scraper.llm.gemini_provider.httpx")
    def test_is_available_failure(self, mock_httpx, mock_chat_cls):
        """is_available returns False when API is unreachable."""
        from agentic_scraper.llm.gemini_provider import GeminiProvider

        mock_httpx.get.side_effect = ConnectionError("unreachable")
        provider = GeminiProvider(
            api_key="test-key", model="gemini-2.5-flash", temperature=0.3
        )
        assert provider.is_available() is False

    @patch("agentic_scraper.llm.gemini_provider.ChatGoogleGenerativeAI")
    def test_satisfies_llm_provider_protocol(self, mock_chat_cls):
        """GeminiProvider satisfies the LLMProvider protocol."""
        from agentic_scraper.llm.gemini_provider import GeminiProvider
        from agentic_scraper.llm.provider import LLMProvider

        provider = GeminiProvider(
            api_key="test-key", model="gemini-2.5-flash", temperature=0.3
        )
        assert isinstance(provider, LLMProvider)
