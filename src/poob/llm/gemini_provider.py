"""Google Gemini-backed LLM provider using LangChain's ChatGoogleGenerativeAI."""

from __future__ import annotations

import httpx
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_google_genai import ChatGoogleGenerativeAI


class GeminiProvider:
    """LLM provider backed by Google Gemini API.

    Used for world-knowledge tasks (retail lookup, category estimation)
    where a cloud model with broad training data outperforms local models.

    Args:
        api_key: Google API key.
        model: Gemini model name (e.g. 'gemini-2.5-flash').
        temperature: Sampling temperature.
    """

    def __init__(self, api_key: str, model: str, temperature: float) -> None:
        self._model_name = model
        self._api_key = api_key
        self._chat_model = ChatGoogleGenerativeAI(
            model=model,
            google_api_key=api_key,
            temperature=temperature,
        )

    @property
    def chat_model(self) -> BaseChatModel:
        """Return the LangChain ChatGoogleGenerativeAI model."""
        return self._chat_model

    @property
    def model_name(self) -> str:
        """Return the model name (e.g. 'gemini-2.5-flash')."""
        return self._model_name

    def is_available(self) -> bool:
        """Check if the Gemini API is reachable with the given key."""
        try:
            resp = httpx.get(
                f"https://generativelanguage.googleapis.com/v1beta/models?key={self._api_key}",
                timeout=5,
            )
            return resp.status_code == 200
        except Exception:
            return False
