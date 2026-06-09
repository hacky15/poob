"""The brain's Groq client must be fail-fast.

Groq's SDK defaults (max_retries=2, 60s timeout, timeouts retried 2x) mean a
rate-limited or hung Groq call blocks for seconds-to-minutes before the
provider cascade can fall over to Gemini. The brain's resilience layer is the
cascade, not SDK retries — so the shared client factory pins max_retries=0 and
a tight timeout. See docs/decisions/groq-failfast-client.md.
"""

from __future__ import annotations

import pytest

from poob.brain.poob import PoobBrain


def _brain() -> PoobBrain:
    """A bare PoobBrain — _make_groq_client only needs groq_api_key."""
    brain = PoobBrain.__new__(PoobBrain)
    brain.groq_api_key = "test-key"
    return brain


def _capture_groq_kwargs(monkeypatch) -> dict:
    captured: dict = {}

    class _FakeAsyncGroq:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    # The factory does `from groq import AsyncGroq` at call time, so patch the
    # source symbol, not a re-export.
    monkeypatch.setattr("groq.AsyncGroq", _FakeAsyncGroq)
    return captured


def test_groq_client_disables_sdk_retries(monkeypatch) -> None:
    captured = _capture_groq_kwargs(monkeypatch)
    _brain()._make_groq_client(timeout=8.0)
    # max_retries=0 → a 429 raises immediately so the cascade tries the next
    # rung in ~100ms instead of after ~1.5-3s of SDK backoff.
    assert captured["max_retries"] == 0
    assert captured["api_key"] == "test-key"


def test_groq_client_caps_timeout(monkeypatch) -> None:
    captured = _capture_groq_kwargs(monkeypatch)
    _brain()._make_groq_client(timeout=8.0)
    # Not the 60s SDK default — a hung Groq call must not block the voice path.
    assert captured["timeout"] == 8.0


def test_groq_client_timeout_is_required() -> None:
    # timeout is keyword-only and required — no silent fallback to the 60s default.
    with pytest.raises(TypeError):
        _brain()._make_groq_client()  # type: ignore[call-arg]


def test_routing_cascade_uses_failfast_client(monkeypatch) -> None:
    """The tool-routing groq branch builds its client via the fail-fast
    factory (max_retries=0), so a Groq 429 falls through to Gemini fast."""
    captured = _capture_groq_kwargs(monkeypatch)

    brain = _brain()
    # Force the groq branch far enough to construct the client, then bail.
    import asyncio

    class _Boom(Exception):
        pass

    # The fake client has no .chat — the AttributeError/our sentinel proves we
    # got past construction with the right kwargs.
    with pytest.raises(Exception):
        asyncio.run(
            brain._call_provider_with_tools("groq", "openai/gpt-oss-20b", [], [], 100)
        )
    assert captured.get("max_retries") == 0
    assert captured.get("timeout") == 8.0


def test_every_groq_client_in_brain_is_failfast() -> None:
    """No Groq client may use the SDK default max_retries=2 — that default
    caused the 13s 429-backoff spikes on the casual/wrap paths (2026-06-09).
    Every AsyncGroq(...) construction must set max_retries (0 inline, or the
    factory's max_retries=max_retries param)."""
    import inspect
    import re

    import poob.brain.poob as brain_mod

    src = inspect.getsource(brain_mod)
    # match AsyncGroq(...) allowing one level of nested parens (httpx.Timeout etc.)
    constructions = re.findall(r"AsyncGroq\((?:[^()]|\([^()]*\))*\)", src)
    assert constructions, "expected AsyncGroq constructions in poob.py"
    bad = [c for c in constructions if "max_retries" not in c]
    assert not bad, f"non-fail-fast Groq client(s) found: {bad}"
