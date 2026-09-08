"""Provider-agnostic rate-limit cooldown for any provider cascade.

Extracted from PoobBrain's tool-routing circuit breaker
(docs/decisions/provider-circuit-breaker.md), which existed only for the
LLM routing cascade. Any cascade that tries a list of (provider, model)
candidates in order — LLM routing, STT, TTS, VLM — can share this instead
of re-implementing retry-after parsing, clamping, and never-strand
fallback per subsystem. See docs/decisions/provider-cooldown-registry.md.

Usage shape for a cascade:

    for item in registry.filter_active(candidates, key=lambda c: c.model):
        try:
            result = await call(item)
            registry.note_success(item.model)
            return result
        except Exception as exc:
            registry.note_rate_limited(item.model, exc)
            registry.note_timeout(item.model, exc)
            continue  # or raise on the last item
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Callable, Sequence, TypeVar

import httpx

T = TypeVar("T")

# Groq: "Please try again in 25m7.68s." / Gemini: "Please retry in 46.4s."
_RETRY_AFTER_RE = re.compile(
    r"(?:try again|retry) in\s+(?:(\d+)\s*m)?\s*([\d.]+)\s*s", re.IGNORECASE
)


@dataclass
class ProviderCooldownRegistry:
    """Tracks per-key rate-limit cooldowns and consecutive-timeout streaks.

    Keys are caller-chosen strings — typically a model id, or a provider's
    ``name`` property when the model alone isn't unique enough (e.g. STT
    providers key on ``"gemini_stt:gemini-2.5-flash-lite"`` so they never
    collide with the LLM cascade's bare ``"gemini-2.5-flash-lite"``, even
    though both may share the same underlying registry instance).
    """

    cooldown_min_s: float = 5.0
    cooldown_max_s: float = 1800.0  # never strand a model longer than 30 min
    cooldown_default_s: float = 60.0  # rate-limited but no advised time
    # A provider that consistently TIMES OUT is functionally down. One
    # timeout is transient — don't react; N consecutive arm a short fixed
    # cooldown (no server signal exists for "I'm hung", so this one is ours,
    # deliberately brief so a recovered provider rejoins quickly).
    timeout_arm_count: int = 2
    timeout_cooldown_s: float = 120.0
    # A permanent failure (model removed, account not entitled/billed) has
    # no "try again in N" signal and, unlike a rate limit, no reason to
    # believe a SHORT wait helps — defaults to the same ceiling as the
    # longest rate-limit cooldown rather than a short guess.
    permanent_failure_cooldown_s: float = 1800.0

    _cooldown: dict[str, float] = field(default_factory=dict, init=False)
    _timeout_streak: dict[str, int] = field(default_factory=dict, init=False)

    # --- pure classification / parsing (no state) ---------------------------

    @staticmethod
    def is_rate_limit_error(exc: Exception) -> bool:
        """True only for 429 / rate-limit / quota errors — NOT timeouts or
        other failures (those are transient; don't cool the model down)."""
        blob = f"{getattr(exc, 'status_code', '')} {exc}".lower()
        return (
            "429" in blob
            or "rate_limit" in blob
            or "rate limit" in blob
            or "resource_exhausted" in blob
            or "too many requests" in blob
        )

    @classmethod
    def retry_after_seconds(cls, exc: Exception) -> float | None:
        """Server-advised cooldown for a 429: prefer the Retry-After header,
        fall back to the provider's 'try again in 2m5.3s' message. None if
        neither is present (caller applies a conservative default)."""
        resp = getattr(exc, "response", None)
        if resp is not None:
            try:
                hdr = resp.headers.get("retry-after")
            except Exception:
                hdr = None
            if hdr:
                try:
                    return float(hdr)
                except (TypeError, ValueError):
                    pass
        # Search the exception text AND the response body — Gemini's
        # "Please retry in 46.4s" lives in the 429 JSON body, which
        # raise_for_status does not include in str(exc).
        blob = str(exc)
        if resp is not None:
            try:
                blob += " " + resp.text[:2000]
            except Exception:
                pass
        m = _RETRY_AFTER_RE.search(blob)
        if m:
            return float(m.group(1) or 0) * 60.0 + float(m.group(2) or 0)
        return None

    @staticmethod
    def is_timeout_error(exc: Exception) -> bool:
        """True for request timeouts (httpx/asyncio/SDK)."""
        if isinstance(exc, (httpx.TimeoutException, TimeoutError)):
            return True
        return "timeout" in type(exc).__name__.lower()

    @staticmethod
    def is_permanent_failure_error(exc: Exception) -> bool:
        """True for errors that mean "this will never work until a human
        fixes something" — model removed/EOL'd (404/410) or the account
        lacks entitlement/billing for it (403/402) — as opposed to a 429
        rate limit (temporary, server tells us when it clears) or a
        timeout (transient, or the provider is merely hung).

        Confirmed live 2026-09-08: an NVIDIA rung stuck on a model whose
        entire generation was sunset returns 410 on every single call,
        forever, and the routing cascade re-probed it on every worst-case
        traversal because 410 matched neither existing classifier. See
        docs/decisions/disable-dead-vision-and-fallback-model-rungs.md.
        """
        status = getattr(exc, "status_code", None)
        if status is None:
            resp = getattr(exc, "response", None)
            status = getattr(resp, "status_code", None)
        if status in (402, 403, 404, 410):
            return True
        blob = str(exc).lower()
        return any(
            s in blob for s in ("404", "410", "not_found", "payment_required", "payment required")
        )

    # --- stateful cooldown tracking ------------------------------------------

    def in_cooldown(self, key: str) -> bool:
        until = self._cooldown.get(key)
        return until is not None and time.monotonic() < until

    def note_rate_limited(self, key: str, exc: Exception) -> None:
        """Cool a key down after a 429 for its server-advised window
        (clamped). No-op for non-rate-limit errors."""
        if not self.is_rate_limit_error(exc):
            return
        secs = self.retry_after_seconds(exc)
        if secs is None:
            secs = self.cooldown_default_s
        secs = max(self.cooldown_min_s, min(secs, self.cooldown_max_s))
        self._cooldown[key] = time.monotonic() + secs

    def note_permanent_failure(self, key: str, exc: Exception) -> None:
        """Cool a key down for `permanent_failure_cooldown_s` on a
        model-gone / not-entitled error (404/410/403/402). No-op for
        anything else. A long, fixed cooldown rather than a short retry —
        there is no reason a permanent failure clears itself in seconds,
        unlike a rate limit's server-advised window."""
        if not self.is_permanent_failure_error(exc):
            return
        self._cooldown[key] = time.monotonic() + self.permanent_failure_cooldown_s

    def note_timeout(self, key: str, exc: Exception) -> None:
        """Arm a short fixed cooldown after `timeout_arm_count` consecutive
        timeouts. Any non-timeout call (success or a different error type)
        resets the streak."""
        if not self.is_timeout_error(exc):
            self._timeout_streak.pop(key, None)
            return
        n = self._timeout_streak.get(key, 0) + 1
        self._timeout_streak[key] = n
        if n >= self.timeout_arm_count:
            self._cooldown[key] = time.monotonic() + self.timeout_cooldown_s
            self._timeout_streak.pop(key, None)

    def note_success(self, key: str) -> None:
        """Clear cooldown + timeout streak (half-open -> closed) after a
        successful call, so a recovered provider is trusted immediately —
        the whole point of "go right back once it relieves"."""
        self._cooldown.pop(key, None)
        self._timeout_streak.pop(key, None)

    def filter_active(
        self,
        items: Sequence[T],
        key: Callable[[T], str] = lambda x: x,  # type: ignore[assignment]
    ) -> list[T]:
        """Drop cooling items so the cascade skips a known-capped provider
        instead of re-probing it. Never strands the cascade: if every item
        is cooling, returns the full list (least-bad) rather than an empty
        cascade with nothing to try."""
        active = [it for it in items if not self.in_cooldown(key(it))]
        return active if active else list(items)
