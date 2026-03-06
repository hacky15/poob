"""Groq-backed LLM provider using LangChain's ChatGroq.

Groq free tier: 30 RPM, 14,400 RPD (8B models), 1,000 RPD (70B models).
No credit card required. LPU hardware delivers 300+ tok/s.
"""

from __future__ import annotations

import httpx
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_groq import ChatGroq

from agentic_scraper.utils.logging import get_logger

log = get_logger("llm.groq")

GROQ_API_URL = "https://api.groq.com/openai/v1"


class GroqProvider:
    """LLM provider backed by Groq Cloud API.

    Used as a fallback for Cerebras when rate-limited, or as a rotation partner
    to spread cloud calls across providers.

    Args:
        api_key: Groq API key.
        model: Model name (e.g. "llama-3.3-70b-versatile").
        temperature: Sampling temperature.
        max_tokens: Max completion tokens (keep low to avoid TPM pressure).
    """

    def __init__(
        self,
        api_key: str,
        model: str,
        temperature: float,
        max_tokens: int = 500,
    ) -> None:
        self._api_key = api_key
        self._model_name = model
        self._chat_model = ChatGroq(
            model=model,
            api_key=api_key,
            temperature=temperature,
            max_tokens=max_tokens,
            max_retries=0,  # Let with_fallbacks handle, not internal retry
        )

    @property
    def chat_model(self) -> BaseChatModel:
        """Return the LangChain ChatGroq model."""
        return self._chat_model

    @property
    def model_name(self) -> str:
        """Return the model name."""
        return self._model_name

    def is_available(self) -> bool:
        """Check if the Groq API is reachable."""
        try:
            resp = httpx.get(
                f"{GROQ_API_URL}/models",
                headers={"Authorization": f"Bearer {self._api_key}"},
                timeout=5,
            )
            return resp.status_code == 200
        except Exception:
            return False
