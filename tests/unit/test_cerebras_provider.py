"""Tests for CerebrasProvider."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


def _mock_models_response(*model_ids: str) -> MagicMock:
    """Create a mock httpx response for /models endpoint."""
    resp = MagicMock(status_code=200)
    resp.json.return_value = {
        "data": [{"id": mid} for mid in model_ids]
    }
    return resp


def _mock_test_success() -> MagicMock:
    """Create a mock httpx response for a successful test completion."""
    return MagicMock(status_code=200)


def _mock_test_failure() -> MagicMock:
    """Create a mock httpx response for a failed test completion (404)."""
    return MagicMock(status_code=404)


class TestCerebrasProvider:
    """Tests for CerebrasProvider initialization and protocol compliance."""

    @patch("agentic_scraper.llm.cerebras_provider._detect_best_model", return_value="qwen-3-235b")
    @patch("agentic_scraper.llm.cerebras_provider.ChatOpenAI")
    def test_creates_chat_model(self, mock_chat_cls, mock_detect):
        """CerebrasProvider creates a ChatOpenAI instance with Cerebras base URL."""
        from agentic_scraper.llm.cerebras_provider import CEREBRAS_BASE_URL, CerebrasProvider

        provider = CerebrasProvider(
            api_key="test-key", model="qwen-3-235b", temperature=0.3
        )
        mock_chat_cls.assert_called_once_with(
            model="qwen-3-235b",
            api_key="test-key",
            base_url=CEREBRAS_BASE_URL,
            temperature=0.3,
            max_tokens=2048,
            max_retries=0,
        )
        assert provider.chat_model is mock_chat_cls.return_value

    @patch("agentic_scraper.llm.cerebras_provider._detect_best_model", return_value="qwen-3-235b")
    @patch("agentic_scraper.llm.cerebras_provider.ChatOpenAI")
    def test_model_name(self, mock_chat_cls, mock_detect):
        """model_name returns the resolved model string."""
        from agentic_scraper.llm.cerebras_provider import CerebrasProvider

        provider = CerebrasProvider(
            api_key="test-key", model="qwen-3-235b", temperature=0.3
        )
        assert provider.model_name == "qwen-3-235b"

    @patch("agentic_scraper.llm.cerebras_provider._detect_best_model", return_value="qwen-3-235b")
    @patch("agentic_scraper.llm.cerebras_provider.ChatOpenAI")
    @patch("agentic_scraper.llm.cerebras_provider._test_model", return_value=True)
    def test_is_available_success(self, mock_test, mock_chat_cls, mock_detect):
        """is_available returns True when model responds to test call."""
        from agentic_scraper.llm.cerebras_provider import CerebrasProvider

        provider = CerebrasProvider(
            api_key="test-key", model="qwen-3-235b", temperature=0.3
        )
        assert provider.is_available() is True

    @patch("agentic_scraper.llm.cerebras_provider._detect_best_model", return_value="qwen-3-235b")
    @patch("agentic_scraper.llm.cerebras_provider.ChatOpenAI")
    @patch("agentic_scraper.llm.cerebras_provider._test_model", return_value=False)
    def test_is_available_failure(self, mock_test, mock_chat_cls, mock_detect):
        """is_available returns False when model fails test call."""
        from agentic_scraper.llm.cerebras_provider import CerebrasProvider

        provider = CerebrasProvider(
            api_key="test-key", model="qwen-3-235b", temperature=0.3
        )
        assert provider.is_available() is False

    @patch("agentic_scraper.llm.cerebras_provider._detect_best_model", return_value="qwen-3-235b")
    @patch("agentic_scraper.llm.cerebras_provider.ChatOpenAI")
    def test_satisfies_llm_provider_protocol(self, mock_chat_cls, mock_detect):
        """CerebrasProvider satisfies the LLMProvider protocol."""
        from agentic_scraper.llm.cerebras_provider import CerebrasProvider
        from agentic_scraper.llm.provider import LLMProvider

        provider = CerebrasProvider(
            api_key="test-key", model="qwen-3-235b", temperature=0.3
        )
        assert isinstance(provider, LLMProvider)


class TestTestModel:
    """Tests for _test_model inference verification."""

    @patch("agentic_scraper.llm.cerebras_provider.httpx")
    def test_returns_true_on_200(self, mock_httpx):
        """Should return True when model responds with 200."""
        from agentic_scraper.llm.cerebras_provider import _test_model

        mock_httpx.post.return_value = _mock_test_success()
        assert _test_model("key", "gpt-oss-120b") is True

    @patch("agentic_scraper.llm.cerebras_provider.httpx")
    def test_returns_false_on_404(self, mock_httpx):
        """Should return False when model returns 404."""
        from agentic_scraper.llm.cerebras_provider import _test_model

        mock_httpx.post.return_value = _mock_test_failure()
        assert _test_model("key", "nonexistent-model") is False

    @patch("agentic_scraper.llm.cerebras_provider.httpx")
    def test_returns_false_on_exception(self, mock_httpx):
        """Should return False on connection error."""
        from agentic_scraper.llm.cerebras_provider import _test_model

        mock_httpx.post.side_effect = ConnectionError("unreachable")
        assert _test_model("key", "gpt-oss-120b") is False


class TestDetectBestModel:
    """Tests for _detect_best_model auto-detection with inference verification."""

    @patch("agentic_scraper.llm.cerebras_provider._test_model", return_value=True)
    @patch("agentic_scraper.llm.cerebras_provider.httpx")
    def test_returns_preferred_when_it_works(self, mock_httpx, mock_test):
        """Should return the preferred model if it passes inference test."""
        from agentic_scraper.llm.cerebras_provider import _detect_best_model

        mock_httpx.get.return_value = _mock_models_response(
            "llama3.1-8b", "qwen-3-235b-a22b-instruct-2507", "gpt-oss-120b"
        )
        result = _detect_best_model("key", preferred="qwen-3-235b-a22b-instruct-2507")
        assert result == "qwen-3-235b-a22b-instruct-2507"

    @patch("agentic_scraper.llm.cerebras_provider._test_model")
    @patch("agentic_scraper.llm.cerebras_provider.httpx")
    def test_skips_preferred_when_404(self, mock_httpx, mock_test):
        """Should skip preferred model if it fails inference and use next best."""
        from agentic_scraper.llm.cerebras_provider import _detect_best_model

        mock_httpx.get.return_value = _mock_models_response(
            "llama3.1-8b", "qwen-3-235b-a22b-instruct-2507", "gpt-oss-120b"
        )
        # Preferred fails, gpt-oss-120b works
        mock_test.side_effect = lambda key, model: model != "qwen-3-235b-a22b-instruct-2507"
        result = _detect_best_model("key", preferred="qwen-3-235b-a22b-instruct-2507")
        assert result == "gpt-oss-120b"

    @patch("agentic_scraper.llm.cerebras_provider._test_model")
    @patch("agentic_scraper.llm.cerebras_provider.httpx")
    def test_falls_through_priority_list(self, mock_httpx, mock_test):
        """Should try models in priority order until one works."""
        from agentic_scraper.llm.cerebras_provider import _detect_best_model

        mock_httpx.get.return_value = _mock_models_response(
            "llama3.1-8b", "gpt-oss-120b"
        )
        # Only llama3.1-8b works
        mock_test.side_effect = lambda key, model: model == "llama3.1-8b"
        result = _detect_best_model("key", preferred="qwen-3-235b")
        assert result == "llama3.1-8b"

    @patch("agentic_scraper.llm.cerebras_provider._test_model", return_value=False)
    @patch("agentic_scraper.llm.cerebras_provider.httpx")
    def test_returns_none_when_nothing_works(self, mock_httpx, mock_test):
        """Should return None if no model passes inference test."""
        from agentic_scraper.llm.cerebras_provider import _detect_best_model

        mock_httpx.get.return_value = _mock_models_response("model-a", "model-b")
        result = _detect_best_model("key", preferred=None)
        assert result is None

    @patch("agentic_scraper.llm.cerebras_provider._test_model", return_value=True)
    @patch("agentic_scraper.llm.cerebras_provider.httpx")
    def test_falls_back_to_priority_list_on_api_error(self, mock_httpx, mock_test):
        """Should try priority list models if /models endpoint fails."""
        from agentic_scraper.llm.cerebras_provider import _detect_best_model

        mock_httpx.get.side_effect = ConnectionError("unreachable")
        result = _detect_best_model("key", preferred="my-model")
        # Should try preferred first since it's added before priority list
        assert result == "my-model"


class TestAutoDetectIntegration:
    """Tests for auto-detection wired into CerebrasProvider."""

    @patch("agentic_scraper.llm.cerebras_provider.ChatOpenAI")
    @patch("agentic_scraper.llm.cerebras_provider._test_model")
    @patch("agentic_scraper.llm.cerebras_provider.httpx")
    def test_auto_selects_working_model(self, mock_httpx, mock_test, mock_chat_cls):
        """Should use the first model that passes inference test."""
        from agentic_scraper.llm.cerebras_provider import CEREBRAS_BASE_URL, CerebrasProvider

        mock_httpx.get.return_value = _mock_models_response(
            "gpt-oss-120b", "qwen-3-235b-a22b-instruct-2507", "llama3.1-8b"
        )
        # Only gpt-oss-120b works
        mock_test.side_effect = lambda key, model: model == "gpt-oss-120b"

        provider = CerebrasProvider(
            api_key="test-key",
            model="qwen-3-235b-a22b-instruct-2507",  # Configured but doesn't work
            temperature=0.3,
        )
        assert provider.model_name == "gpt-oss-120b"
        mock_chat_cls.assert_called_once_with(
            model="gpt-oss-120b",
            api_key="test-key",
            base_url=CEREBRAS_BASE_URL,
            temperature=0.3,
            max_tokens=2048,
            max_retries=0,
        )

    @patch("agentic_scraper.llm.cerebras_provider.ChatOpenAI")
    @patch("agentic_scraper.llm.cerebras_provider._test_model", return_value=True)
    @patch("agentic_scraper.llm.cerebras_provider.httpx")
    def test_uses_configured_when_it_works(self, mock_httpx, mock_test, mock_chat_cls):
        """Should use configured model when it passes inference test."""
        from agentic_scraper.llm.cerebras_provider import CerebrasProvider

        mock_httpx.get.return_value = _mock_models_response(
            "gpt-oss-120b", "qwen-3-235b-a22b-instruct-2507"
        )
        provider = CerebrasProvider(
            api_key="test-key",
            model="qwen-3-235b-a22b-instruct-2507",
            temperature=0.3,
        )
        assert provider.model_name == "qwen-3-235b-a22b-instruct-2507"
