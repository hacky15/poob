"""Rate-limit-aware VLM provider cascade.

Tries VLM providers in priority order, automatically falling through
on HTTP 429 or quota exhaustion.  Tracks daily usage per provider to
proactively avoid hitting limits.

Provider priority (configurable):
  1. Gemini 2.5 Flash (10-15 RPM, 250 RPD free)
  2. Groq Llama 4 Scout VLM (30 RPM, 14,400 RPD free)
  3. Gemini 2.5 Pro (5 RPM, 100 RPD free)
  4. OpenRouter free tier (varies)
  5. Ollama qwen2.5vl (unlimited, local, slower)
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from agentic_scraper.utils.logging import get_logger

if TYPE_CHECKING:
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.messages import BaseMessage

log = get_logger("llm.vlm_cascade")


class AllProvidersExhaustedError(Exception):
    """Raised when every VLM provider in the cascade has failed or is exhausted."""


@dataclass
class _DailyCounter:
    """Track daily API calls for a single provider."""

    date: str = ""
    count: int = 0

    def increment(self) -> None:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self.date != today:
            self.date = today
            self.count = 0
        self.count += 1

    def get_count(self) -> int:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self.date != today:
            return 0
        return self.count


@dataclass
class VLMProviderConfig:
    """Configuration for a single VLM provider in the cascade.

    Attributes:
        name: Human-readable provider name (e.g. "gemini_flash").
        chat_model: LangChain chat model instance.
        daily_limit: Maximum requests per day (0 = unlimited).
        supports_images: Whether the model supports image inputs.
    """

    name: str
    chat_model: BaseChatModel
    daily_limit: int = 0
    supports_images: bool = True


class VLMCascade:
    """Rate-limit-aware VLM provider cascade.

    Tries providers in priority order.  On HTTP 429 or quota exhaustion,
    automatically falls through to the next provider.  Tracks daily usage
    per provider to proactively avoid hitting limits.

    Args:
        providers: Ordered list of VLM provider configs (highest priority first).
    """

    def __init__(self, providers: list[VLMProviderConfig]) -> None:
        self._providers = providers
        self._daily_counts: dict[str, _DailyCounter] = {
            p.name: _DailyCounter() for p in providers
        }
        # Track providers that hit 429 recently (name → timestamp)
        self._rate_limited_until: dict[str, float] = {}

    async def invoke(
        self,
        messages: list[BaseMessage],
        *,
        skill: str = "vlm_eval",
    ) -> Any:
        """Try each provider in order until one succeeds.

        Args:
            messages: LangChain messages (may include image content).
            skill: Skill name for logging context.

        Returns:
            The AIMessage response from the first successful provider.

        Raises:
            AllProvidersExhaustedError: If all providers fail or are exhausted.
        """
        errors: list[str] = []
        now = time.monotonic()

        for provider in self._providers:
            name = provider.name

            # Skip if proactively rate-limited
            if self._is_quota_exhausted(provider):
                log.debug("vlm.skip_exhausted", provider=name)
                continue

            # Skip if recently 429'd (cool down for 60s)
            cooldown_until = self._rate_limited_until.get(name, 0.0)
            if now < cooldown_until:
                log.debug(
                    "vlm.skip_cooldown",
                    provider=name,
                    remaining_s=round(cooldown_until - now, 1),
                )
                continue

            try:
                t0 = time.monotonic()
                response = await provider.chat_model.ainvoke(messages)
                elapsed = time.monotonic() - t0

                self._daily_counts[name].increment()
                log.info(
                    "vlm.success",
                    provider=name,
                    skill=skill,
                    elapsed_s=round(elapsed, 1),
                    response_chars=len(str(response.content)) if response.content else 0,
                )
                return response

            except Exception as exc:
                elapsed = time.monotonic() - t0
                exc_str = str(exc)[:200]

                # Detect rate limit errors (HTTP 429)
                if _is_rate_limit_error(exc):
                    self._rate_limited_until[name] = time.monotonic() + 60.0
                    log.warning(
                        "vlm.rate_limited",
                        provider=name,
                        skill=skill,
                        elapsed_s=round(elapsed, 1),
                    )
                else:
                    log.warning(
                        "vlm.error",
                        provider=name,
                        skill=skill,
                        elapsed_s=round(elapsed, 1),
                        error=exc_str,
                    )
                errors.append(f"{name}: {exc_str}")

        raise AllProvidersExhaustedError(
            f"All {len(self._providers)} VLM providers exhausted. "
            f"Errors: {'; '.join(errors)}"
        )

    def _is_quota_exhausted(self, provider: VLMProviderConfig) -> bool:
        """Check if a provider's daily quota is already used up."""
        if provider.daily_limit <= 0:
            return False  # Unlimited
        count = self._daily_counts[provider.name].get_count()
        return count >= provider.daily_limit

    def get_provider_stats(self) -> dict[str, dict]:
        """Return current usage stats for all providers (for monitoring)."""
        stats = {}
        for p in self._providers:
            counter = self._daily_counts[p.name]
            stats[p.name] = {
                "daily_count": counter.get_count(),
                "daily_limit": p.daily_limit,
                "rate_limited": time.monotonic() < self._rate_limited_until.get(p.name, 0.0),
            }
        return stats

    def get_available_provider_name(self) -> str | None:
        """Return the name of the next available provider, or None."""
        now = time.monotonic()
        for p in self._providers:
            if self._is_quota_exhausted(p):
                continue
            if now < self._rate_limited_until.get(p.name, 0.0):
                continue
            return p.name
        return None


def _is_rate_limit_error(exc: BaseException) -> bool:
    """Detect HTTP 429 / rate-limit errors across different provider SDKs."""
    exc_str = str(exc).lower()
    # Common patterns across OpenAI-compatible, Google, Groq SDKs
    if "429" in exc_str:
        return True
    if "rate limit" in exc_str or "rate_limit" in exc_str:
        return True
    if "quota" in exc_str and "exceeded" in exc_str:
        return True
    if "resource_exhausted" in exc_str:
        return True
    # Check for status_code attribute (httpx, requests)
    status = getattr(exc, "status_code", None)
    if status == 429:
        return True
    return False


def build_vlm_cascade(config: Any) -> VLMCascade:
    """Build the VLM provider cascade from application config.

    Instantiates available providers based on configured API keys and
    returns them in priority order.

    Args:
        config: AppConfig instance.

    Returns:
        VLMCascade with all available providers.
    """
    providers: list[VLMProviderConfig] = []

    # 1. Gemini Flash (primary VLM evaluator)
    if config.google_api_key:
        from langchain_google_genai import ChatGoogleGenerativeAI

        flash = ChatGoogleGenerativeAI(
            model=config.gemini_flash_model,
            google_api_key=config.google_api_key,
            temperature=0.1,
            max_retries=0,
        )
        providers.append(
            VLMProviderConfig(
                name="gemini_flash",
                chat_model=flash,
                daily_limit=config.gemini_flash_rpd,
            )
        )

    # 2. Groq Vision (Llama 4 Scout)
    if config.groq_api_key:
        from langchain_groq import ChatGroq

        groq_vlm = ChatGroq(
            model=config.groq_vision_model,
            api_key=config.groq_api_key,
            temperature=0.1,
            max_tokens=1000,
            max_retries=0,
        )
        providers.append(
            VLMProviderConfig(
                name="groq_vision",
                chat_model=groq_vlm,
                daily_limit=14400,
            )
        )

    # 3. Gemini Pro (second opinion)
    if config.google_api_key:
        from langchain_google_genai import ChatGoogleGenerativeAI

        pro = ChatGoogleGenerativeAI(
            model=config.gemini_pro_model,
            google_api_key=config.google_api_key,
            temperature=0.1,
            max_retries=0,
        )
        providers.append(
            VLMProviderConfig(
                name="gemini_pro",
                chat_model=pro,
                daily_limit=config.gemini_pro_rpd,
            )
        )

    # 4. OpenRouter free tier
    if config.openrouter_api_key and config.openrouter_enabled:
        from langchain_openai import ChatOpenAI

        openrouter = ChatOpenAI(
            model=config.openrouter_model,
            api_key=config.openrouter_api_key,
            base_url="https://openrouter.ai/api/v1",
            temperature=0.1,
            max_tokens=1000,
            max_retries=0,
        )
        providers.append(
            VLMProviderConfig(
                name="openrouter",
                chat_model=openrouter,
                daily_limit=0,  # Varies, let 429 detection handle it
            )
        )

    # 5. Local Ollama VLM (failsafe, unlimited)
    from langchain_ollama import ChatOllama

    ollama_vlm = ChatOllama(
        model=config.vision_model,
        base_url=config.ollama_base_url,
        temperature=0.1,
        num_ctx=config.vision_model_num_ctx,
    )
    providers.append(
        VLMProviderConfig(
            name="ollama_vision",
            chat_model=ollama_vlm,
            daily_limit=0,
        )
    )

    log.info(
        "vlm_cascade.built",
        providers=[p.name for p in providers],
        count=len(providers),
    )
    return VLMCascade(providers)
