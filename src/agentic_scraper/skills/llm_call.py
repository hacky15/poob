"""Instrumented LLM call wrapper for skills.

Wraps ``BaseChatModel.ainvoke`` with structured logging that captures:
- Which model/provider is handling the request
- Wall-clock latency
- Response size (chars)
- Errors with timing context
- Timeout enforcement (prevents indefinite hangs from unresponsive providers)

Usage in a skill::

    from agentic_scraper.skills.llm_call import llm_call

    response = await llm_call(self._llm, [HumanMessage(content=prompt)], skill="identify")
    # Custom timeout:
    response = await llm_call(llm, messages, skill="triage", timeout_s=60)
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

from agentic_scraper.utils.logging import get_logger

if TYPE_CHECKING:
    from langchain_core.messages import BaseMessage

log = get_logger("llm.call")

# Default timeout — generous enough for slow providers (Ollama on CPU,
# Gemini thinking models) but catches indefinite hangs like the 86-minute
# Groq stall observed in production.
_DEFAULT_TIMEOUT_S: float = 120.0


def _model_id(llm: Any, _depth: int = 0) -> str:
    """Extract a readable model identifier from a LangChain model."""
    if _depth > 3:
        return type(llm).__name__
    # Direct model attributes (ChatOllama, ChatOpenAI, ChatGroq, etc.)
    for attr in ("model_name", "model"):
        val = getattr(llm, attr, None)
        if val and isinstance(val, str):
            return val
    # Fallback chain (RunnableWithFallbacksT) — show the primary model
    runnable = getattr(llm, "runnable", None)
    if runnable and not isinstance(runnable, type(llm)):
        return f"{_model_id(runnable, _depth + 1)}+fallbacks"
    # Bound model (RunnableBinding from .bind_tools)
    bound = getattr(llm, "bound", None)
    if bound and not isinstance(bound, type(llm)):
        return _model_id(bound, _depth + 1)
    return type(llm).__name__


async def llm_call(
    llm: Any,
    messages: list[BaseMessage],
    *,
    skill: str,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
) -> Any:
    """Invoke an LLM with timing, provider logging, and timeout enforcement.

    Args:
        llm: LangChain chat model (or fallback chain).
        messages: Messages to send.
        skill: Name of the calling skill (for log context).
        timeout_s: Maximum wall-clock seconds before aborting. Defaults to 120s.

    Returns:
        The LLM AIMessage response.

    Raises:
        TimeoutError: If the call exceeds ``timeout_s``.
        Any exception from the underlying LLM.
    """
    model = _model_id(llm)
    prompt_chars = sum(len(str(m.content)) for m in messages)
    t0 = time.monotonic()
    log.info("llm.request", skill=skill, model=model, prompt_chars=prompt_chars)
    try:
        response = await asyncio.wait_for(
            llm.ainvoke(messages), timeout=timeout_s,
        )
        elapsed = time.monotonic() - t0
        resp_len = len(str(response.content)) if response.content else 0
        log.info(
            "llm.response",
            skill=skill,
            model=model,
            elapsed_s=round(elapsed, 1),
            response_chars=resp_len,
        )
        return response
    except asyncio.TimeoutError:
        elapsed = time.monotonic() - t0
        log.warning(
            "llm.timeout",
            skill=skill,
            model=model,
            elapsed_s=round(elapsed, 1),
            timeout_s=timeout_s,
        )
        raise TimeoutError(
            f"LLM call to {model} timed out after {timeout_s:.0f}s (skill={skill})"
        )
    except Exception as exc:
        elapsed = time.monotonic() - t0
        log.warning(
            "llm.error",
            skill=skill,
            model=model,
            elapsed_s=round(elapsed, 1),
            error=str(exc)[:200],
        )
        raise
