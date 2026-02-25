"""LLM provider protocol and factory function."""

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
    """Factory: reads config.llm_provider and returns the right implementation."""
    if config.llm_provider == "ollama":
        from .ollama_provider import OllamaProvider

        return OllamaProvider(
            model=config.ollama_model,
            base_url=config.ollama_base_url,
            temperature=config.llm_temperature,
        )
    raise ValueError(f"Unknown LLM provider: {config.llm_provider}")
