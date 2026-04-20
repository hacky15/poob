"""OpenRouter-backed LLM provider for multi-model access.

OpenRouter aggregates multiple model providers behind a single API,
offering free tiers for several models.  Uses OpenAI-compatible API format.
"""

from __future__ import annotations

import httpx
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_openai import ChatOpenAI

from poob.utils.logging import get_logger

log = get_logger("llm.openrouter")

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


class OpenRouterProvider:
    """LLM provider backed by OpenRouter's multi-model API.

    Used as a VLM fallback in the cascade when primary providers
    (Gemini, Groq) are rate-limited.

    Args:
        api_key: OpenRouter API key.
        model: Model identifier (e.g. "meta-llama/llama-4-scout-17b-16e-instruct:free").
        temperature: Sampling temperature.
        max_tokens: Max completion tokens.
    """

    def __init__(
        self,
        api_key: str,
        model: str,
        temperature: float = 0.1,
        max_tokens: int = 1000,
    ) -> None:
        self._api_key = api_key
        self._model_name = model
        self._chat_model = ChatOpenAI(
            model=model,
            api_key=api_key,
            base_url=OPENROUTER_BASE_URL,
            temperature=temperature,
            max_tokens=max_tokens,
            max_retries=0,
        )

    @property
    def chat_model(self) -> BaseChatModel:
        """Return the LangChain ChatOpenAI model configured for OpenRouter."""
        return self._chat_model

    @property
    def model_name(self) -> str:
        """Return the model name."""
        return self._model_name

    def is_available(self) -> bool:
        """Check if the OpenRouter API is reachable."""
        try:
            resp = httpx.get(
                f"{OPENROUTER_BASE_URL}/models",
                headers={"Authorization": f"Bearer {self._api_key}"},
                timeout=5,
            )
            return resp.status_code == 200
        except Exception:
            return False
