"""Cerebras-backed LLM provider using LangChain's ChatOpenAI (OpenAI-compatible API).

Cerebras free tier: 30 RPM, 60K TPM, 1M tokens/day.
Auto-detects the best available model by testing actual inference.
"""

from __future__ import annotations

import httpx
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_openai import ChatOpenAI

from poob.utils.logging import get_logger

log = get_logger("llm.cerebras")

# Cerebras API base URL (OpenAI-compatible)
CEREBRAS_BASE_URL = "https://api.cerebras.ai/v1"

# Preferred models in priority order (largest/best first).
# The provider will try each until one actually works.
_PREFERRED_MODELS = [
    "qwen-3-235b-a22b-instruct-2507",
    "qwen-3-coder-480b",
    "gpt-oss-120b",
    "llama-3.3-70b",
    "qwen-3-32b",
    "llama3.1-8b",
]

# Models below this threshold are not worth using on Cerebras — Groq's 70B
# is a better fallback than Cerebras's 8B.  When only weak models are
# available, the provider reports itself as unavailable so the cascade skips
# to Groq.
_MIN_QUALITY_MODELS = {
    "qwen-3-235b-a22b-instruct-2507",
    "qwen-3-coder-480b",
    "gpt-oss-120b",
    "llama-3.3-70b",
    "qwen-3-32b",
}


def _test_model(api_key: str, model_id: str) -> bool:
    """Send a tiny test completion to verify a model actually works.

    The /models endpoint can list models that return 404 on inference.
    This catches that by doing a real (minimal) API call.

    Args:
        api_key: Cerebras API key.
        model_id: Model ID to test.

    Returns:
        True if the model responded successfully.
    """
    try:
        resp = httpx.post(
            f"{CEREBRAS_BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": model_id,
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 1,
            },
            timeout=5,
        )
        if resp.status_code == 200:
            return True
        log.debug("Model test failed", model=model_id, status=resp.status_code)
        return False
    except Exception:
        return False


def _detect_best_model(api_key: str, preferred: str | None = None) -> str | None:
    """Find the best Cerebras model that actually works for inference.

    Lists available models, then tests each candidate with a real API call.

    Args:
        api_key: Cerebras API key.
        preferred: If set, try this model first.

    Returns:
        Model ID string, or None if no model works.
    """
    # Get candidate list from /models
    candidates: list[str] = []
    try:
        resp = httpx.get(
            f"{CEREBRAS_BASE_URL}/models",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            available_ids: set[str] = set()
            for model in data.get("data", []):
                model_id = model.get("id", "")
                if model_id:
                    available_ids.add(model_id)

            log.info("Cerebras models listed", models=sorted(available_ids))

            # Build ordered candidate list: preferred first, then priority, then rest
            if preferred and preferred in available_ids:
                candidates.append(preferred)
            for model_id in _PREFERRED_MODELS:
                if model_id in available_ids and model_id not in candidates:
                    candidates.append(model_id)
            for model_id in sorted(available_ids):
                if model_id not in candidates:
                    candidates.append(model_id)
        else:
            log.warning("Cerebras /models returned non-200", status=resp.status_code)
            # Fall back to trying preferred + priority list without /models
            if preferred:
                candidates.append(preferred)
            candidates.extend(_PREFERRED_MODELS)

    except Exception as exc:
        log.warning("Failed to list Cerebras models", error=str(exc))
        if preferred:
            candidates.append(preferred)
        candidates.extend(_PREFERRED_MODELS)

    # Test each candidate with a real inference call
    for model_id in candidates:
        log.info("Testing Cerebras model", model=model_id)
        if _test_model(api_key, model_id):
            log.info("Cerebras model verified", model=model_id)
            return model_id
        log.warning("Cerebras model not usable", model=model_id)

    log.error("No Cerebras models available for inference")
    return None


class CerebrasProvider:
    """LLM provider backed by Cerebras Cloud API.

    Used for world-knowledge tasks (retail lookup, category estimation)
    where a large model with broad training data outperforms local 8B models.
    Free tier: 30 RPM, 60K TPM, 1M tokens/day — more than enough for deal evaluation.

    Auto-detects the best available model by testing actual inference calls.

    Args:
        api_key: Cerebras API key.
        model: Preferred model name. Auto-detection picks the best if unavailable.
        temperature: Sampling temperature.
    """

    def __init__(self, api_key: str, model: str, temperature: float) -> None:
        self._api_key = api_key

        # Auto-detect best available model via real inference test
        detected = _detect_best_model(api_key, preferred=model)
        if detected and detected != model:
            log.info(
                "Cerebras model auto-selected",
                configured=model,
                using=detected,
            )
        elif not detected:
            log.error("No Cerebras model works, using configured as fallback", model=model)

        self._model_name = detected or model
        self._quality_ok = self._model_name in _MIN_QUALITY_MODELS

        self._chat_model = ChatOpenAI(
            model=self._model_name,
            api_key=api_key,
            base_url=CEREBRAS_BASE_URL,
            temperature=temperature,
            max_tokens=2048,  # Triage batches need ~800-1200 tokens for 5 listings
            max_retries=0,    # Let with_fallbacks handle, not internal retry
        )

    @property
    def chat_model(self) -> BaseChatModel:
        """Return the LangChain ChatOpenAI model configured for Cerebras."""
        return self._chat_model

    @property
    def model_name(self) -> str:
        """Return the resolved model name."""
        return self._model_name

    def is_available(self) -> bool:
        """Check if the Cerebras API is reachable with a quality model.

        Returns False if only weak models (e.g. 8B) are available — Groq's
        70B is a better fallback than Cerebras's 8B.
        """
        if not self._quality_ok:
            log.info(
                "Cerebras skipped (model below quality threshold)",
                model=self._model_name,
            )
            return False
        return True  # Already verified during __init__ auto-detection
