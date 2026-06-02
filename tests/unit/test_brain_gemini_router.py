"""Tests for the Gemini tool-router rung in PoobBrain's cascade.

Gemini (via its OpenAI-compatible endpoint) is RPD-limited with NO daily
token cap, so it carries tool routing when Groq's per-day token cap is spent
mid-session — the failure mode that degraded routing to the weaker NVIDIA
rung. See docs/decisions/gemini-tool-router-rung.md and
docs/gotchas/groq-daily-token-cap-degrades-routing.md.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from poob.brain.poob import PoobBrain


def _brain(**kw: Any) -> PoobBrain:
    return PoobBrain(deal_agent=None, **kw)


@pytest.mark.asyncio
async def test_gemini_branch_parses_openai_tool_call() -> None:
    """The gemini branch hits the OpenAI-compat endpoint with Bearer auth and
    parses a standard tool_calls response into (text, name, args)."""
    brain = _brain(google_api_key="g-key")
    tools = [{"type": "function", "function": {"name": "music_assistant", "parameters": {}}}]

    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value={
        "choices": [{"message": {"tool_calls": [
            {"function": {"name": "music_assistant",
                          "arguments": '{"action":"play","query":"tiki tiki"}'}}
        ]}}]
    })
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.post = AsyncMock(return_value=resp)

    with patch("poob.brain.poob.httpx.AsyncClient", return_value=client):
        text, name, args = await brain._call_provider_with_tools(
            "gemini", "gemini-2.5-flash-lite",
            [{"role": "user", "content": "play tiki tiki"}], tools, 256,
        )

    assert text == ""
    assert name == "music_assistant"
    assert args == {"action": "play", "query": "tiki tiki"}
    # Reached Gemini's OpenAI-compatible endpoint.
    url = client.post.call_args.args[0]
    assert "generativelanguage.googleapis.com" in url
    assert "/openai/chat/completions" in url
    assert client.post.call_args.kwargs["headers"]["Authorization"] == "Bearer g-key"


@pytest.mark.asyncio
async def test_gemini_sits_between_groq_and_nvidia() -> None:
    """When Groq errors (e.g. daily-cap 429) the cascade advances to Gemini
    BEFORE NVIDIA — Gemini is the reliable, no-token-cap rung."""
    brain = _brain(groq_api_key="gq", google_api_key="gg", nvidia_api_key="nv")
    calls: list[str] = []

    async def fake_call(provider, model, *a, **kw):  # type: ignore[no-untyped-def]
        calls.append(provider)
        if provider == "groq":
            raise RuntimeError("429 tokens per day")
        return "", "music_assistant", {"action": "play", "query": "x"}

    with patch.object(brain, "_call_provider_with_tools", new=fake_call):
        text, name, args = await brain._groq_with_tools(
            [{"role": "user", "content": "play x"}], 256,
        )

    assert name == "music_assistant"
    assert calls[0] == "groq"
    assert calls[1] == "gemini"      # Gemini before NVIDIA
    assert "nvidia" not in calls     # Gemini succeeded → NVIDIA never reached


@pytest.mark.asyncio
async def test_gemini_uses_configured_router_model() -> None:
    """The cascade calls Gemini with gemini_router_model, not some other slot."""
    brain = _brain(groq_api_key="gq", google_api_key="gg",
                   gemini_router_model="gemini-2.5-flash-lite")
    seen: dict[str, str] = {}

    async def fake_call(provider, model, *a, **kw):  # type: ignore[no-untyped-def]
        seen[provider] = model
        if provider == "groq":
            raise RuntimeError("down")
        return "", "music_assistant", {"action": "play", "query": "x"}

    with patch.object(brain, "_call_provider_with_tools", new=fake_call):
        await brain._groq_with_tools([{"role": "user", "content": "play x"}], 256)

    assert seen["gemini"] == "gemini-2.5-flash-lite"


@pytest.mark.asyncio
async def test_gemini_absent_without_key() -> None:
    """No google_api_key → Gemini is not in the cascade at all."""
    brain = _brain(groq_api_key="gq", nvidia_api_key="nv")  # no google key
    calls: list[str] = []

    async def fake_call(provider, model, *a, **kw):  # type: ignore[no-untyped-def]
        calls.append(provider)
        raise RuntimeError("down")

    with patch.object(brain, "_call_provider_with_tools", new=fake_call):
        with pytest.raises(Exception):
            await brain._groq_with_tools([{"role": "user", "content": "hi"}], 256)

    assert "gemini" not in calls
