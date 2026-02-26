"""Ollama-backed LLM provider using LangChain's ChatOllama."""

from __future__ import annotations

import httpx
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_ollama import ChatOllama


class OllamaProvider:
    """LLM provider backed by a local Ollama instance.

    Args:
        model: Ollama model name (e.g. 'qwen3:8b').
        base_url: Ollama API base URL.
        temperature: Sampling temperature.
        format: Output format constraint (e.g. 'json'). None for free-form.
        num_ctx: Context window size. None uses Ollama default.
    """

    def __init__(
        self,
        model: str,
        base_url: str,
        temperature: float,
        format: str | None = None,
        num_ctx: int | None = None,
    ) -> None:
        self._model_name = model
        self._base_url = base_url

        kwargs: dict[str, object] = {
            "model": model,
            "base_url": base_url,
            "temperature": temperature,
        }
        if format is not None:
            kwargs["format"] = format
        if num_ctx is not None:
            kwargs["num_ctx"] = num_ctx

        self._chat_model = ChatOllama(**kwargs)

    @property
    def chat_model(self) -> BaseChatModel:
        """Return the LangChain ChatOllama model."""
        return self._chat_model

    @property
    def model_name(self) -> str:
        """Return the model name (e.g. 'llama3.1:8b')."""
        return self._model_name

    def is_available(self) -> bool:
        """Check if Ollama is running and reachable."""
        try:
            resp = httpx.get(f"{self._base_url}/api/tags", timeout=5)
            return resp.status_code == 200
        except Exception:
            return False
