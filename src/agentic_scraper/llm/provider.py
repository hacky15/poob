"""LLM provider protocol and factory functions."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from langchain_core.language_models.chat_models import BaseChatModel

if TYPE_CHECKING:
    from agentic_scraper.config import AppConfig


@runtime_checkable
class LLMProvider(Protocol):
    """Protocol for LLM backends.

    Swap Ollama for OpenAI/Anthropic by implementing this protocol.
    """

    @property
    def chat_model(self) -> BaseChatModel:
        """Return a LangChain-compatible chat model instance."""
        ...

    @property
    def model_name(self) -> str:
        """Human-readable model identifier."""
        ...

    def is_available(self) -> bool:
        """Health check: can we reach the LLM right now?"""
        ...


def create_llm_provider(config: AppConfig) -> LLMProvider:
    """Factory: reads config.llm_provider and returns the right implementation.

    This creates the *browser* LLM (no format constraint, used for browser-use agents).
    """
    if config.llm_provider == "ollama":
        from .ollama_provider import OllamaProvider

        return OllamaProvider(
            model=config.ollama_model,
            base_url=config.ollama_base_url,
            temperature=config.llm_temperature,
        )
    if config.llm_provider == "gemini":
        from .gemini_provider import GeminiProvider

        return GeminiProvider(
            api_key=config.google_api_key,
            model=config.google_model,
            temperature=config.llm_temperature,
        )
    raise ValueError(f"Unknown LLM provider: {config.llm_provider}")


def create_cloud_provider(config: AppConfig) -> LLMProvider | None:
    """Create a cloud LLM provider for world-knowledge tasks.

    Returns None if no Google API key is configured, allowing
    graceful fallback to local models.
    """
    if not config.google_api_key:
        return None

    from .gemini_provider import GeminiProvider

    return GeminiProvider(
        api_key=config.google_api_key,
        model=config.google_model,
        temperature=config.llm_temperature,
    )


def create_knowledge_provider(config: AppConfig) -> LLMProvider | None:
    """Create a cloud LLM provider for world-knowledge tasks.

    Priority: Cerebras (free, 235B model) > Gemini > None (falls back to Ollama).

    Cerebras free tier: 30 RPM, 60K TPM, 1M tokens/day — more than enough
    for retail price lookups and category estimation.

    Returns None if no cloud API key is configured.
    """
    if config.cerebras_api_key:
        from .cerebras_provider import CerebrasProvider

        return CerebrasProvider(
            api_key=config.cerebras_api_key,
            model=config.cerebras_model,
            temperature=config.llm_temperature,
        )

    if config.google_api_key:
        from .gemini_provider import GeminiProvider

        return GeminiProvider(
            api_key=config.google_api_key,
            model=config.google_model,
            temperature=config.llm_temperature,
        )

    return None
