"""Tests for OllamaProvider."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


class TestOllamaProvider:
    """Tests for OllamaProvider initialization and format/num_ctx support."""

    @patch("agentic_scraper.llm.ollama_provider.ChatOllama")
    def test_basic_init(self, mock_chat_cls):
        """OllamaProvider creates ChatOllama with basic args."""
        from agentic_scraper.llm.ollama_provider import OllamaProvider

        provider = OllamaProvider(
            model="qwen3:8b",
            base_url="http://localhost:11434",
            temperature=0.3,
        )
        mock_chat_cls.assert_called_once_with(
            model="qwen3:8b",
            base_url="http://localhost:11434",
            temperature=0.3,
        )

    @patch("agentic_scraper.llm.ollama_provider.ChatOllama")
    def test_format_json_passthrough(self, mock_chat_cls):
        """format='json' is passed through to ChatOllama."""
        from agentic_scraper.llm.ollama_provider import OllamaProvider

        OllamaProvider(
            model="qwen3:8b",
            base_url="http://localhost:11434",
            temperature=0.0,
            format="json",
        )
        mock_chat_cls.assert_called_once_with(
            model="qwen3:8b",
            base_url="http://localhost:11434",
            temperature=0.0,
            format="json",
        )

    @patch("agentic_scraper.llm.ollama_provider.ChatOllama")
    def test_num_ctx_passthrough(self, mock_chat_cls):
        """num_ctx is passed through to ChatOllama."""
        from agentic_scraper.llm.ollama_provider import OllamaProvider

        OllamaProvider(
            model="qwen3:8b",
            base_url="http://localhost:11434",
            temperature=0.0,
            num_ctx=12000,
        )
        mock_chat_cls.assert_called_once_with(
            model="qwen3:8b",
            base_url="http://localhost:11434",
            temperature=0.0,
            num_ctx=12000,
        )

    @patch("agentic_scraper.llm.ollama_provider.ChatOllama")
    def test_format_and_num_ctx_combined(self, mock_chat_cls):
        """Both format and num_ctx can be set together."""
        from agentic_scraper.llm.ollama_provider import OllamaProvider

        OllamaProvider(
            model="qwen3:8b",
            base_url="http://localhost:11434",
            temperature=0.0,
            format="json",
            num_ctx=12000,
        )
        mock_chat_cls.assert_called_once_with(
            model="qwen3:8b",
            base_url="http://localhost:11434",
            temperature=0.0,
            format="json",
            num_ctx=12000,
        )

    @patch("agentic_scraper.llm.ollama_provider.ChatOllama")
    def test_none_format_omitted(self, mock_chat_cls):
        """format=None should not pass 'format' key to ChatOllama."""
        from agentic_scraper.llm.ollama_provider import OllamaProvider

        OllamaProvider(
            model="qwen3:8b",
            base_url="http://localhost:11434",
            temperature=0.3,
            format=None,
            num_ctx=None,
        )
        call_kwargs = mock_chat_cls.call_args[1]
        assert "format" not in call_kwargs
        assert "num_ctx" not in call_kwargs

    @patch("agentic_scraper.llm.ollama_provider.ChatOllama")
    def test_model_name_property(self, mock_chat_cls):
        """model_name returns the configured model string."""
        from agentic_scraper.llm.ollama_provider import OllamaProvider

        provider = OllamaProvider(
            model="qwen3:8b",
            base_url="http://localhost:11434",
            temperature=0.3,
        )
        assert provider.model_name == "qwen3:8b"


class TestProviderFactory:
    """Tests for the provider factory functions."""

    @patch("agentic_scraper.llm.ollama_provider.ChatOllama")
    def test_create_llm_provider_ollama(self, mock_chat_cls):
        """create_llm_provider returns OllamaProvider for 'ollama'."""
        from agentic_scraper.config import AppConfig
        from agentic_scraper.llm.ollama_provider import OllamaProvider
        from agentic_scraper.llm.provider import create_llm_provider

        config = AppConfig(
            discord_bot_token="test",
            discord_deals_channel_id=1,
            llm_provider="ollama",
        )
        provider = create_llm_provider(config)
        assert isinstance(provider, OllamaProvider)

    def test_create_llm_provider_unknown(self):
        """create_llm_provider raises ValueError for unknown provider."""
        from agentic_scraper.config import AppConfig
        from agentic_scraper.llm.provider import create_llm_provider

        config = AppConfig(
            discord_bot_token="test",
            discord_deals_channel_id=1,
            llm_provider="unknown",
        )
        with pytest.raises(ValueError, match="Unknown LLM provider"):
            create_llm_provider(config)

    def test_create_cloud_provider_no_key(self):
        """create_cloud_provider returns None when no API key is set."""
        from agentic_scraper.config import AppConfig
        from agentic_scraper.llm.provider import create_cloud_provider

        config = AppConfig(
            discord_bot_token="test",
            discord_deals_channel_id=1,
            google_api_key="",
        )
        assert create_cloud_provider(config) is None

    @patch("agentic_scraper.llm.gemini_provider.ChatGoogleGenerativeAI")
    def test_create_cloud_provider_with_key(self, mock_chat_cls):
        """create_cloud_provider returns GeminiProvider when key is set."""
        from agentic_scraper.config import AppConfig
        from agentic_scraper.llm.gemini_provider import GeminiProvider
        from agentic_scraper.llm.provider import create_cloud_provider

        config = AppConfig(
            discord_bot_token="test",
            discord_deals_channel_id=1,
            google_api_key="test-api-key",
        )
        provider = create_cloud_provider(config)
        assert isinstance(provider, GeminiProvider)
        assert provider.model_name == "gemini-2.5-flash"
