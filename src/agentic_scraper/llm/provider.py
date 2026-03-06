"""LLM provider protocol and factory functions."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from langchain_core.language_models.chat_models import BaseChatModel

from agentic_scraper.utils.logging import get_logger

if TYPE_CHECKING:
    from agentic_scraper.config import AppConfig

log = get_logger("llm.provider")


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


def build_cloud_llm_with_fallbacks(
    config: AppConfig,
    local_fallback: BaseChatModel,
) -> BaseChatModel:
    """Build a cloud LLM chain with automatic fallbacks.

    Priority: Cerebras → Groq → local Ollama.
    Uses LangChain's ``with_fallbacks`` so a 429 or error on the primary
    transparently retries on the next provider.

    Args:
        config: Application configuration with API keys.
        local_fallback: Local Ollama model as last resort.

    Returns:
        A BaseChatModel (possibly wrapped with fallbacks).
    """
    models: list[BaseChatModel] = []
    names: list[str] = []

    # Cerebras: highest-param model, 1K true RPM with max_tokens capped
    if config.cerebras_api_key:
        from .cerebras_provider import CerebrasProvider

        provider = CerebrasProvider(
            api_key=config.cerebras_api_key,
            model=config.cerebras_model,
            temperature=config.ollama_json_temperature,
        )
        if provider.is_available():
            models.append(provider.chat_model)
            names.append(f"cerebras:{provider.model_name}")

    # Groq: fast LPU inference, 30 RPM, good JSON adherence
    if config.groq_api_key:
        from .groq_provider import GroqProvider

        provider = GroqProvider(
            api_key=config.groq_api_key,
            model=config.groq_model,
            temperature=config.ollama_json_temperature,
        )
        if provider.is_available():
            models.append(provider.chat_model)
            names.append(f"groq:{provider.model_name}")

    # Local Ollama as last resort
    models.append(local_fallback)
    names.append("ollama:local")

    log.info("Cloud LLM fallback chain built", providers=names)

    primary = models[0]
    if len(models) > 1:
        return primary.with_fallbacks(models[1:])
    return primary
