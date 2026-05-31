"""Rate-limit-aware VLM provider cascade with hybrid voting.

Phase A: Parallel diverse evaluation (3 architecturally diverse providers)
Phase B: Supermajority early exit (all 3 agree → accept immediately)
Phase C: Disagreement escalation (Gemini Pro as weighted tiebreaker)

Falls back to sequential cascade when voting is disabled or insufficient
providers are available. Tracks daily usage per provider to proactively
avoid hitting limits.

Provider priority (ranked by Vision Arena score, free tier only):
  1. Gemini 3 Flash (rank 4, score 1274, 250 RPD free) — Google architecture
  2. Gemini 3.1 Flash Lite (rank 35, score 1188, 1000 RPD free) — Google Lite
  3. Gemma 3 27B (rank 50, score 1158, 1000 RPD free) — Google/Gemma
  4. Groq Llama 4 Scout VLM (rank 63, score 1128, 14,400 RPD free) — Meta
  5. OpenRouter Mistral Small 3.1 24B (rank 64, score 1128, free) — Mistral
  6. Gemini 2.5 Pro (rank 9, score 1248, 100 RPD — tiebreaker only)
  7. OpenRouter Nemotron Nano 12B v2 VL (free, extra fallback) — NVIDIA
  8. Ollama qwen2.5vl:7b (unlimited, local)
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from statistics import mean, stdev
from typing import TYPE_CHECKING, Any

from poob.utils.content import extract_json, extract_text
from poob.utils.logging import get_logger

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
        architecture: Model architecture family for diversity grouping.
        is_tiebreaker: Whether this provider is reserved for tiebreaking.
    """

    name: str
    chat_model: BaseChatModel
    daily_limit: int = 0
    supports_images: bool = True
    architecture: str = "unknown"
    is_tiebreaker: bool = False
    ipm_limit: int = 0  # Images per minute limit (0 = unlimited)


@dataclass
class VotingResult:
    """Result from the parallel voting phase.

    Attributes:
        responses: List of (provider_name, response_content) tuples.
        agreement: Whether all voters agreed on deal quality.
        confidence: Agreement-based confidence (1.0 - coefficient of variation).
    """

    responses: list[tuple[str, str]] = field(default_factory=list)
    agreement: bool = False
    confidence: float = 0.0


class VLMCascade:
    """Hybrid cascade+voting VLM provider system.

    Supports two modes:
    1. **Voting mode** (default when ≥3 providers available):
       - Phase A: Query 3 diverse providers in parallel
       - Phase B: Supermajority exit if all agree
       - Phase C: Tiebreaker escalation on disagreement
    2. **Sequential cascade** (fallback):
       - Try providers in order until one succeeds

    Args:
        providers: Ordered list of VLM provider configs (highest priority first).
        voting_enabled: Enable parallel voting mode.
        voting_panel_size: Number of providers in the voting panel.
        value_agreement_threshold: Max % deviation for value agreement (0.15 = 15%).
    """

    # Consecutive timeout/error threshold before demoting a provider from panels.
    # At 2, a provider is demoted after ~70s of wasted timeout (2 × 35s).
    _CONSECUTIVE_FAIL_THRESHOLD = 2

    # Seconds before a demoted provider gets another chance.
    # 10 minutes is long enough to outlast transient rate limits but short
    # enough to recover within a single patrol session.
    _DEMOTION_COOLDOWN_S = 600.0

    def __init__(
        self,
        providers: list[VLMProviderConfig],
        *,
        voting_enabled: bool = True,
        voting_panel_size: int = 3,
        value_agreement_threshold: float = 0.15,
        kv_store: Any | None = None,
    ) -> None:
        self._providers = providers
        self._daily_counts: dict[str, _DailyCounter] = {
            p.name: _DailyCounter() for p in providers
        }
        self._consecutive_failures: dict[str, int] = {p.name: 0 for p in providers}
        # Timestamp when a provider was first demoted (for cooldown recovery)
        self._demoted_at: dict[str, float] = {}
        # IPM (images per minute) tracking — deque of monotonic timestamps
        # per provider, tracking when images were sent in the last 60 seconds.
        self._ipm_windows: dict[str, deque[float]] = {
            p.name: deque() for p in providers
        }
        self._tiebreaker_lock = asyncio.Lock()
        self._voting_enabled = voting_enabled
        self._voting_panel_size = voting_panel_size
        self._value_agreement_threshold = value_agreement_threshold
        # Last voting result — available to callers after invoke()
        self.last_agreement_confidence: float | None = None
        # Names of providers that responded in the last invoke()
        self.last_responding_providers: list[str] = []
        # KV store for persisting tiebreaker cooldown across restarts
        self._kv = kv_store

        # Restore rate-limit cooldowns (especially tiebreaker) from KV
        self._rate_limited_until: dict[str, float] = {}
        if self._kv:
            now = time.monotonic()
            for p in providers:
                if p.is_tiebreaker:
                    expiry = self._kv.get_float(f"vlm_rate_limit_until_{p.name}", 0.0)
                    if expiry > now:
                        # Convert from wall-clock seconds to monotonic equivalent
                        # (stored as wall-clock, convert delta to current monotonic)
                        wall_now = __import__("time").time()
                        wall_expiry = self._kv.get_float(
                            f"vlm_rate_limit_wall_{p.name}", 0.0
                        )
                        if wall_expiry > wall_now:
                            remaining = wall_expiry - wall_now
                            self._rate_limited_until[p.name] = now + remaining
                            log.info(
                                "vlm.tiebreaker_cooldown_restored",
                                provider=p.name,
                                remaining_s=round(remaining),
                            )

    async def invoke(
        self,
        messages: list[BaseMessage],
        *,
        skill: str = "vlm_eval",
    ) -> Any:
        """Try voting first, fall back to sequential cascade.

        Args:
            messages: LangChain messages (may include image content).
            skill: Skill name for logging context.

        Returns:
            The AIMessage response from the best provider.

        Raises:
            AllProvidersExhaustedError: If all providers fail or are exhausted.
        """
        # Try voting mode if enabled and enough providers available
        if self._voting_enabled and skill == "vlm_eval":
            available = self._get_available_providers(exclude_tiebreakers=True)
            if len(available) >= self._voting_panel_size:
                try:
                    return await self._invoke_voting(messages, available, skill)
                except AllProvidersExhaustedError:
                    pass  # Fall through to sequential

        # Sequential cascade fallback
        return await self._invoke_sequential(messages, skill)

    async def _invoke_voting(
        self,
        messages: list[BaseMessage],
        available: list[VLMProviderConfig],
        skill: str,
    ) -> Any:
        """Phase A+B+C: Parallel voting with supermajority exit.

        Selects architecturally diverse providers for the voting panel.
        On agreement, returns the best response. On disagreement,
        escalates to a tiebreaker provider.
        """
        # Select diverse panel (prefer different architectures)
        panel = self._select_diverse_panel(available)
        log.info(
            "vlm.voting_start",
            panel=[p.name for p in panel],
            skill=skill,
        )

        # Phase A: Query panel in parallel with early exit.
        # Instead of waiting 35s for ALL providers (wasting time on slow ones),
        # collect results as they arrive and exit as soon as 2 providers agree.
        tasks = {
            provider.name: asyncio.create_task(
                self._invoke_single(provider, messages, skill)
            )
            for provider in panel
        }
        task_to_name = {t: n for n, t in tasks.items()}

        responses: dict[str, Any] = {}
        errors: list[str] = []
        remaining = set(tasks.values())
        deadline = time.monotonic() + 35.0

        while remaining:
            timeout_left = deadline - time.monotonic()
            if timeout_left <= 0:
                break

            # Wait for the next task to finish
            done, remaining = await asyncio.wait(
                remaining,
                timeout=timeout_left,
                return_when=asyncio.FIRST_COMPLETED,
            )

            # Collect results from completed tasks
            for task in done:
                name = task_to_name[task]
                if task.cancelled():
                    errors.append(f"{name}: cancelled")
                    continue
                exc = task.exception()
                if exc:
                    errors.append(f"{name}: {str(exc)[:100]}")
                    if _is_permanent_error(exc):
                        # Disable for 24h — this error won't resolve with retries
                        self._rate_limited_until[name] = (
                            time.monotonic() + 86400.0
                        )
                        log.warning(
                            "vlm.provider_permanently_disabled",
                            provider=name,
                            reason=str(exc)[:120],
                        )
                    elif _is_rate_limit_error(exc):
                        provider_cfg = next(
                            (p for p in self._providers if p.name == name), None
                        )
                        cooldown = (
                            3600.0
                            if provider_cfg and 0 < provider_cfg.daily_limit <= 250
                            else 120.0
                        )
                        self._rate_limited_until[name] = time.monotonic() + cooldown
                else:
                    responses[name] = task.result()

            # Early exit: if we have 2+ responses and they agree, skip the rest
            if len(responses) >= 2:
                agreement = self._check_agreement(responses)
                if agreement > 0:
                    break

        # Cancel any still-pending tasks (don't waste time).
        # NOTE: Do NOT increment consecutive_failures for early-exit cancels.
        # These providers didn't fail — voting reached consensus early.
        # Incrementing here causes false demotions of healthy providers.
        for task in remaining:
            name = task_to_name[task]
            task.cancel()
            errors.append(f"{name}: cancelled (early exit or timeout)")

        if errors:
            log.warning(
                "vlm.voting_partial_failures",
                failures=errors,
                succeeded=list(responses.keys()),
            )

        if not responses:
            raise AllProvidersExhaustedError(
                f"All voting panel providers failed: {'; '.join(errors)}"
            )

        # Record which providers responded (for provenance)
        self.last_responding_providers = list(responses.keys())

        # If only 1 response, use it directly
        if len(responses) == 1:
            name, resp = next(iter(responses.items()))
            log.info("vlm.voting_single_response", provider=name)
            self.last_agreement_confidence = None
            return resp

        # Phase B: Check for agreement (unanimous or majority)
        agreement = self._check_agreement(responses)
        self.last_agreement_confidence = agreement if agreement else 0.0
        if agreement:
            # Pick the best response from the agreeing group.
            # For unanimous (1.0/0.7), any response works — use first by priority.
            # For majority (0.5), pick the first responder from the majority group.
            best_name = self._pick_majority_response(responses, agreement)
            log.info(
                "vlm.voting_agreement",
                providers=list(responses.keys()),
                confidence=round(agreement, 2),
            )
            return responses[best_name]

        # Phase C: Disagreement — escalate to tiebreaker (serialized via lock)
        async with self._tiebreaker_lock:
            tiebreaker = self._get_tiebreaker()
            if tiebreaker:
                try:
                    log.info("vlm.voting_tiebreaker", provider=tiebreaker.name)
                    tiebreaker_resp = await self._invoke_single(
                        tiebreaker, messages, skill
                    )
                    return tiebreaker_resp
                except Exception as exc:
                    if _is_permanent_error(exc):
                        cooldown = 86400.0
                    elif _is_rate_limit_error(exc):
                        cooldown = 3600.0
                    else:
                        cooldown = 60.0
                    self._rate_limited_until[tiebreaker.name] = (
                        time.monotonic() + cooldown
                    )
                    if self._kv:
                        self._kv.set_float(
                            f"vlm_rate_limit_wall_{tiebreaker.name}",
                            time.time() + cooldown,
                        )
                    log.warning(
                        "vlm.tiebreaker_failed",
                        error=str(exc)[:100],
                        cooldown_s=cooldown,
                    )

        # No tiebreaker available — use first successful response
        first_name = next(iter(responses))
        log.info(
            "vlm.voting_no_consensus",
            using=first_name,
            voter_count=len(responses),
        )
        return responses[first_name]

    async def _invoke_sequential(
        self,
        messages: list[BaseMessage],
        skill: str,
    ) -> Any:
        """Sequential cascade: try providers in order until one succeeds."""
        errors: list[str] = []

        for provider in self._providers:
            name = provider.name
            if not self._is_available(provider):
                continue

            try:
                result = await self._invoke_single(provider, messages, skill)
                self.last_responding_providers = [name]
                return result
            except Exception as exc:
                exc_str = str(exc)[:200]
                if _is_permanent_error(exc):
                    self._rate_limited_until[name] = time.monotonic() + 86400.0
                    log.warning(
                        "vlm.provider_permanently_disabled",
                        provider=name,
                        skill=skill,
                        reason=exc_str[:120],
                    )
                elif _is_rate_limit_error(exc):
                    cooldown = (
                        3600.0
                        if 0 < provider.daily_limit <= 250
                        else 120.0
                    )
                    self._rate_limited_until[name] = time.monotonic() + cooldown
                    if self._kv and provider.is_tiebreaker:
                        self._kv.set_float(
                            f"vlm_rate_limit_wall_{name}",
                            time.time() + cooldown,
                        )
                    log.warning(
                        "vlm.rate_limited",
                        provider=name,
                        skill=skill,
                        cooldown_s=cooldown,
                    )
                else:
                    log.warning("vlm.error", provider=name, skill=skill, error=exc_str)
                errors.append(f"{name}: {exc_str}")

        raise AllProvidersExhaustedError(
            f"All {len(self._providers)} VLM providers exhausted. "
            f"Errors: {'; '.join(errors)}"
        )

    async def _invoke_single(
        self,
        provider: VLMProviderConfig,
        messages: list[BaseMessage],
        skill: str,
    ) -> Any:
        """Invoke a single provider and track usage."""
        # Count the attempt upfront so daily counter stays accurate
        self._daily_counts[provider.name].increment()

        try:
            t0 = time.monotonic()
            response = await provider.chat_model.ainvoke(messages)
            elapsed = time.monotonic() - t0

            # Check for empty responses — some providers (e.g. OpenRouter
            # Nemotron) return HTTP 200 but no content. Treat as failure
            # so they don't pollute the voting panel.
            content_text = extract_text(response.content) if response.content else ""
            if not content_text.strip():
                self._consecutive_failures[provider.name] = (
                    self._consecutive_failures.get(provider.name, 0) + 1
                )
                log.warning(
                    "vlm.empty_response",
                    provider=provider.name,
                    skill=skill,
                    elapsed_s=round(elapsed, 1),
                )
                raise ValueError(f"{provider.name} returned empty response")

            # Success — reset consecutive failure counter and record IPM
            self._consecutive_failures[provider.name] = 0
            if provider.ipm_limit > 0:
                # Count images in the messages (each HumanMessage with image content)
                image_count = sum(
                    1 for msg in messages
                    if hasattr(msg, "content") and isinstance(msg.content, list)
                    for part in msg.content
                    if isinstance(part, dict) and part.get("type") == "image_url"
                )
                now_mono = time.monotonic()
                window = self._ipm_windows.setdefault(provider.name, deque())
                for _ in range(max(image_count, 1)):
                    window.append(now_mono)
            log.info(
                "vlm.success",
                provider=provider.name,
                skill=skill,
                elapsed_s=round(elapsed, 1),
                response_chars=len(content_text),
            )
            return response
        except Exception:
            self._consecutive_failures[provider.name] = (
                self._consecutive_failures.get(provider.name, 0) + 1
            )
            raise

    def _select_diverse_panel(
        self, available: list[VLMProviderConfig]
    ) -> list[VLMProviderConfig]:
        """Select architecturally diverse providers for the voting panel.

        Prefers providers from different architecture families to minimize
        correlated errors (e.g. Google + Meta + Alibaba).
        """
        panel: list[VLMProviderConfig] = []
        seen_architectures: set[str] = set()

        # First pass: one from each architecture
        for p in available:
            if p.architecture not in seen_architectures:
                panel.append(p)
                seen_architectures.add(p.architecture)
                if len(panel) >= self._voting_panel_size:
                    return panel

        # Second pass: fill remaining slots regardless of architecture
        for p in available:
            if p not in panel:
                panel.append(p)
                if len(panel) >= self._voting_panel_size:
                    return panel

        return panel

    def _check_agreement(self, responses: dict[str, Any]) -> float:
        """Check if voting responses agree on deal quality.

        Parses JSON from each response and compares deal_quality fields.
        Supports both unanimous and majority (2/3+) agreement.

        Returns:
            1.0 — unanimous quality + value agreement
            0.7 — unanimous quality, values differ
            0.5 — majority quality agreement (2/3+)
            0.0 — no agreement
        """
        qualities: list[str] = []
        values: list[float] = []

        for name, resp in responses.items():
            content = extract_text(resp.content) if resp.content else ""
            data = extract_json(content)
            if data is None:
                continue
            quality = str(data.get("deal_quality", "")).lower()
            if quality:
                qualities.append(quality)
            try:
                mid_val = float(data.get("estimated_value_mid", 0))
                if mid_val > 0:
                    values.append(mid_val)
            except (ValueError, TypeError):
                pass

        if len(qualities) < 2:
            return 0.0

        # Check deal_quality agreement — unanimous first, then majority
        quality_counts: dict[str, int] = {}
        for q in qualities:
            quality_counts[q] = quality_counts.get(q, 0) + 1

        unanimous = len(set(qualities)) == 1
        # Majority: most common quality has > half the votes (2/3, 3/5, etc.)
        majority_quality = max(quality_counts, key=quality_counts.get)  # type: ignore[arg-type]
        majority_count = quality_counts[majority_quality]
        has_majority = majority_count > len(qualities) / 2

        # Check value agreement (coefficient of variation < threshold)
        value_agreement = True
        if len(values) >= 2 and mean(values) > 0:
            cv = stdev(values) / mean(values)
            value_agreement = cv < self._value_agreement_threshold

        if unanimous and value_agreement:
            return 1.0
        elif unanimous:
            return 0.7
        elif has_majority:
            return 0.5
        else:
            return 0.0

    def _pick_majority_response(
        self, responses: dict[str, Any], agreement: float
    ) -> str:
        """Pick the best response from the agreeing group.

        For unanimous agreement (>=0.7), returns the first provider by priority.
        For majority agreement (0.5), finds the majority quality and returns
        the first provider that voted with the majority.
        """
        if agreement >= 0.7:
            return next(iter(responses))

        # Majority vote — find which quality won and pick from that group
        quality_by_provider: dict[str, str] = {}
        for name, resp in responses.items():
            content = extract_text(resp.content) if resp.content else ""
            data = extract_json(content)
            if data:
                quality_by_provider[name] = str(
                    data.get("deal_quality", "")
                ).lower()

        quality_counts: dict[str, int] = {}
        for q in quality_by_provider.values():
            quality_counts[q] = quality_counts.get(q, 0) + 1

        majority_quality = max(quality_counts, key=quality_counts.get)  # type: ignore[arg-type]

        # Return first provider (by priority order) that agrees with majority
        for name in responses:
            if quality_by_provider.get(name) == majority_quality:
                return name

        # Fallback (shouldn't happen)
        return next(iter(responses))

    def _get_tiebreaker(self) -> VLMProviderConfig | None:
        """Get the tiebreaker provider (typically Gemini Pro)."""
        for p in self._providers:
            if p.is_tiebreaker and self._is_available(p):
                return p
        return None

    def _is_available(self, provider: VLMProviderConfig) -> bool:
        """Check if a provider is available (not exhausted, rate-limited, or flaky).

        Demoted providers recover after ``_DEMOTION_COOLDOWN_S`` seconds so that
        transient rate limits or intermittent errors don't permanently remove a
        provider for the entire session.
        """
        if self._is_quota_exhausted(provider):
            return False
        now = time.monotonic()
        if now < self._rate_limited_until.get(provider.name, 0.0):
            return False
        # IPM (images per minute) check — prevent bursting past provider limits
        if provider.ipm_limit > 0:
            window = self._ipm_windows.get(provider.name, deque())
            # Prune entries older than 60 seconds
            while window and now - window[0] > 60.0:
                window.popleft()
            if len(window) >= provider.ipm_limit:
                return False
        # Demote providers with too many consecutive failures (timeouts, errors)
        failures = self._consecutive_failures.get(provider.name, 0)
        if failures >= self._CONSECUTIVE_FAIL_THRESHOLD:
            # Record demotion timestamp on first check after threshold
            if provider.name not in self._demoted_at:
                self._demoted_at[provider.name] = now
            # Allow recovery after cooldown
            demoted_at = self._demoted_at[provider.name]
            if now - demoted_at < self._DEMOTION_COOLDOWN_S:
                return False
            # Cooldown expired — give provider another chance
            self._consecutive_failures[provider.name] = 0
            del self._demoted_at[provider.name]
            log.info(
                "vlm.provider_recovery",
                provider=provider.name,
                was_failures=failures,
                cooldown_s=self._DEMOTION_COOLDOWN_S,
            )
        return True

    def _get_available_providers(
        self, exclude_tiebreakers: bool = False
    ) -> list[VLMProviderConfig]:
        """Get all currently available providers."""
        available = []
        for p in self._providers:
            if exclude_tiebreakers and p.is_tiebreaker:
                continue
            if self._is_available(p):
                available.append(p)
        return available

    def _is_quota_exhausted(self, provider: VLMProviderConfig) -> bool:
        """Check if a provider's daily quota is already used up."""
        if provider.daily_limit <= 0:
            return False
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
                "consecutive_failures": self._consecutive_failures.get(p.name, 0),
                "demoted": (
                    self._consecutive_failures.get(p.name, 0)
                    >= self._CONSECUTIVE_FAIL_THRESHOLD
                ),
                "demoted_at": self._demoted_at.get(p.name),
                "architecture": p.architecture,
                "is_tiebreaker": p.is_tiebreaker,
            }
        return stats

    def get_available_provider_name(self) -> str | None:
        """Return the name of the next available provider, or None."""
        for p in self._providers:
            if self._is_available(p):
                return p.name
        return None


def _is_rate_limit_error(exc: BaseException) -> bool:
    """Detect HTTP 429 / rate-limit errors across different provider SDKs."""
    # Permanent errors should NOT be treated as transient rate limits
    if _is_permanent_error(exc):
        return False
    exc_str = str(exc).lower()
    if "429" in exc_str:
        return True
    if "rate limit" in exc_str or "rate_limit" in exc_str:
        return True
    if "quota" in exc_str and "exceeded" in exc_str:
        return True
    if "resource_exhausted" in exc_str:
        return True
    status = getattr(exc, "status_code", None)
    if status == 429:
        return True
    return False


def _is_permanent_error(exc: BaseException) -> bool:
    """Detect errors that will never resolve with retries.

    These indicate the provider is permanently unusable for this session:
    - 402 "Credit limit exceeded" (Together.ai — no payment method)
    - 404 "No endpoints found" (OpenRouter — model removed/renamed)
    - 404 "NOT_FOUND" / "is not found for API version" (Google — retired model id)
    - 401 "Unauthorized" (invalid API key)

    Providers with permanent errors are disabled for 24h (effectively
    the entire session) rather than the 120s transient rate-limit cooldown.
    """
    exc_str = str(exc).lower()
    status = getattr(exc, "status_code", None)
    # Together.ai / other 402 billing errors
    if status == 402 or ("402" in exc_str and "credit" in exc_str):
        return True
    if "credit limit" in exc_str:
        return True
    # Model not found / removed / renamed. OpenRouter exposes status_code or
    # "no endpoints"; Google (langchain-google-genai) raises "404 NOT_FOUND" or
    # "... is not found for API version ..." with NO status_code attribute — so
    # match the message shape too, else a retired Google model id loops forever
    # on the 600s recovery cooldown instead of being parked for the session.
    if status == 404:
        return True
    if "404" in exc_str and (
        "no endpoints" in exc_str or "not_found" in exc_str or "not found" in exc_str
    ):
        return True
    if "is not found for api version" in exc_str:
        return True
    # Invalid API key
    if status == 401 or "unauthorized" in exc_str or "invalid api key" in exc_str:
        return True
    return False


def build_vlm_cascade(config: Any, *, kv_store: Any | None = None) -> VLMCascade:
    """Build the VLM provider cascade from application config.

    Instantiates available providers based on configured API keys and
    returns them in priority order with architecture diversity tags
    for the voting system.

    Args:
        config: AppConfig instance.

    Returns:
        VLMCascade with all available providers and voting enabled.
    """
    providers: list[VLMProviderConfig] = []

    # 1. Gemini 3 Flash (Vision Arena rank 4, score 1274 — top free VLM)
    if config.google_api_key:
        from langchain_google_genai import ChatGoogleGenerativeAI

        flash = ChatGoogleGenerativeAI(
            model=config.gemini_flash_model,
            google_api_key=config.google_api_key,
            temperature=0.1,
            max_retries=0,
            # Suppress thinking/reasoning on preview models — we only need
            # the JSON output, and thinking adds 10-20s latency + 4-14K chars.
            model_kwargs={"thinking_config": {"type": "disabled"}},
        )
        providers.append(
            VLMProviderConfig(
                name="gemini_flash",
                chat_model=flash,
                daily_limit=config.gemini_flash_rpd,
                architecture="google",
                ipm_limit=8,  # Google's undocumented IPM limit (~2-10/min free)
            )
        )

    # 2. Gemini 3.1 Flash Lite (rank 35, score 1188, 1000 RPD free — high-volume voter)
    if config.google_api_key and getattr(config, "gemini_flash_lite_model", None):
        from langchain_google_genai import ChatGoogleGenerativeAI

        flash_lite = ChatGoogleGenerativeAI(
            model=config.gemini_flash_lite_model,
            google_api_key=config.google_api_key,
            temperature=0.1,
            max_retries=0,
        )
        providers.append(
            VLMProviderConfig(
                name="gemini_flash_lite",
                chat_model=flash_lite,
                daily_limit=getattr(config, "gemini_flash_lite_rpd", 1000),
                architecture="google_lite",
                ipm_limit=12,  # Slightly higher IPM than Flash
            )
        )

    # 3. Groq Vision (Llama 4 Scout 17B, rank 63, score 1128 — fast, 14,400 RPD)
    # Moved above Gemma: Groq responds in 0.5-1.5s vs Gemma's 5-12s, making it
    # a better primary voter. Gemma was getting cancelled on every voting round
    # because Groq + Flash Lite reached consensus before Gemma finished.
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
                architecture="meta",
            )
        )

    # 4. Mistral Pixtral 12B (1 RPS, unstated daily — stable, predictable)
    # NOTE: Mistral's API rejects "max_tokens" (422 extra_forbidden).
    # Use langchain-mistralai if installed, else ChatOpenAI without max_tokens.
    mistral_api_key = getattr(config, "mistral_api_key", None)
    if mistral_api_key:
        mistral_vlm = None
        try:
            from langchain_mistralai import ChatMistralAI

            mistral_vlm = ChatMistralAI(
                model="pixtral-12b-2409",
                api_key=mistral_api_key,
                temperature=0.1,
                max_tokens=1000,
                max_retries=0,
            )
        except ImportError:
            from langchain_openai import ChatOpenAI

            # ChatOpenAI with Mistral: omit max_tokens to avoid 422
            mistral_vlm = ChatOpenAI(
                model="pixtral-12b-2409",
                api_key=mistral_api_key,
                base_url="https://api.mistral.ai/v1",
                temperature=0.1,
                max_retries=0,
            )
        providers.append(
            VLMProviderConfig(
                name="mistral_pixtral",
                chat_model=mistral_vlm,
                daily_limit=1400,  # ~1 RPS * 60 * 24 conservatively
                architecture="mistral_native",
            )
        )

    # Three permanently-dead VLM rungs were removed in the 2026-05-30 cascade
    # cleanup (retired / credit-limited / removed-upstream models that were
    # masked only by early-exit consensus). The healthy top-3 — groq_vision
    # (also the Meta-architecture voter), gemini_flash_lite, mistral_pixtral —
    # carry delivered deals; the Nemotron rung below covers the OpenRouter/NVIDIA
    # fallback. See docs/decisions/vlm-cascade-dead-rung-cleanup.md.

    # 8. Gemini 2.5 Pro (rank 9, score 1248 — strong but Google-pool quota-limited)
    # No longer the reserved tiebreaker: it was 100%-429 on disagreements (100 RPD
    # shared with the flash-lite workhorse), so disagreements never actually
    # tie-broke. With no tiebreaker, no-consensus uses the first responder — the
    # documented, blessed behavior. Pro stays a normal voter when it has quota.
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
                architecture="google_pro",
                ipm_limit=5,  # Pro has stricter limits
            )
        )

    # 9. OpenRouter Nemotron Nano 12B v2 VL (free, extra fallback)
    if config.openrouter_api_key and config.openrouter_enabled and config.openrouter_model_secondary:
        from langchain_openai import ChatOpenAI

        openrouter_secondary = ChatOpenAI(
            model=config.openrouter_model_secondary,
            api_key=config.openrouter_api_key,
            base_url="https://openrouter.ai/api/v1",
            temperature=0.1,
            max_tokens=1000,
            max_retries=0,
        )
        providers.append(
            VLMProviderConfig(
                name="openrouter_nemotron",
                chat_model=openrouter_secondary,
                daily_limit=200,
                architecture="nvidia",
            )
        )

    # 10. Local Ollama VLM (failsafe, unlimited — upgraded to 7b from 3b)
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
            architecture="local",
        )
    )

    voting_enabled = getattr(config, "vlm_voting_enabled", True)

    log.info(
        "vlm_cascade.built",
        providers=[p.name for p in providers],
        count=len(providers),
        voting_enabled=voting_enabled,
    )
    return VLMCascade(providers, voting_enabled=voting_enabled, kv_store=kv_store)
