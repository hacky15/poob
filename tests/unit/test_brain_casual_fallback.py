"""Tests for the empty-routing-response casual fallback in PoobBrain.

Covers the contract from
docs/decisions/text-casual-fallback-bypass-deal-agent.md:

When ``_groq_with_tools`` returns ``("", None, None)`` (empty content,
no tool — gpt-oss's silent-refusal mode), the brain runs
``_casual_text_fallback`` to get a casual reply from
``llama-3.1-8b-instant`` instead of falling through to the
deal sub-agent (which uses an RLHF-aligned model that refuses
edgy content).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from poob.brain.poob import PoobBrain


def _make_brain(
    *,
    deal_agent: Any | None = None,
) -> PoobBrain:
    """PoobBrain with a fake Groq key so ``self.groq_api_key`` branch runs.

    deal_agent is dataclass-required; pass ``None`` to disable the
    deal-fallback path or an ``AsyncMock`` to assert it doesn't fire.
    """
    return PoobBrain(
        groq_api_key="test-key",
        deal_agent=deal_agent,
    )


# ---------------------------------------------------------------------------
# Empty routing response triggers casual fallback (not deal_agent)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_empty_routing_response_calls_casual_fallback() -> None:
    deal_agent = AsyncMock()
    deal_agent.run = AsyncMock(return_value="DEAL AGENT WAS CALLED")
    brain = _make_brain(deal_agent=deal_agent)

    with patch.object(brain, "_groq_with_tools", new=AsyncMock(return_value=("", None, None))), \
         patch.object(brain, "_casual_text_fallback", new=AsyncMock(return_value="hell yeah, bro")):

        out = await brain.respond("would you smash dyno", user_id="u1", guild_id=10)

    assert out == "hell yeah, bro"
    deal_agent.run.assert_not_called()
    # And history saved the casual response.
    assert brain._histories[(10, "u1")][-1].content == "hell yeah, bro"


@pytest.mark.asyncio
async def test_routing_text_response_regenerates_via_casual_fallback() -> None:
    """Non-empty routing text + no tool MUST be discarded and regenerated
    via the non-RLHF casual model — mirroring the voice path. The routing
    model (gpt-oss-20b) is RLHF-aligned; its prose reaches the user as
    bland-assistant text or refusals if trusted. See
    docs/incidents/text-mode-rlhf-refusal-leak-2026-05-29.md and
    docs/gotchas/empty-routing-response-is-not-failure.md (fall through
    for ANY no-tool case — empty OR text).
    """
    deal_agent = AsyncMock()
    brain = _make_brain(deal_agent=deal_agent)

    with patch.object(brain, "_groq_with_tools", new=AsyncMock(return_value=("yo whats good", None, None))), \
         patch.object(brain, "_casual_text_fallback", new=AsyncMock(return_value="YOOO what's good fam")) as fb:

        out = await brain.respond("yo", user_id="u1", guild_id=10)

    # Routing text is NOT returned verbatim; casual model output is.
    assert out == "YOOO what's good fam"
    assert out != "yo whats good"
    fb.assert_called_once()
    deal_agent.run.assert_not_called()


@pytest.mark.asyncio
async def test_rlhf_refusal_text_never_reaches_user() -> None:
    """A literal RLHF refusal from the routing model must never reach the
    user. This is the exact production failure (2026-05-29): 'Epstein
    files' / 'would you smash Dyno' → 'I'm sorry, but I can't help with
    that.' The non-empty refusal bypassed the empty-only fallback.
    """
    deal_agent = AsyncMock()
    brain = _make_brain(deal_agent=deal_agent)
    refusal = "I'm sorry, but I can't help with that."

    with patch.object(brain, "_groq_with_tools", new=AsyncMock(return_value=(refusal, None, None))), \
         patch.object(brain, "_casual_text_fallback", new=AsyncMock(return_value="pfft, wild question — yeah obviously")) as fb:

        out = await brain.respond("would you smash dyno", user_id="u1", guild_id=10)

    assert refusal not in out
    assert "sorry" not in out.lower()
    fb.assert_called_once()


@pytest.mark.asyncio
async def test_tool_call_skips_casual_fallback() -> None:
    deal_agent = AsyncMock()
    deal_agent.run = AsyncMock(return_value="here are deals")
    brain = _make_brain(deal_agent=deal_agent)

    with patch.object(
        brain, "_groq_with_tools",
        new=AsyncMock(return_value=("", "deal_assistant", {"request": "wishlist"})),
    ), patch.object(brain, "_casual_text_fallback", new=AsyncMock()) as fb:

        out = await brain.respond("whats on my wishlist", user_id="u1", guild_id=10)

    assert "here are deals" in out
    fb.assert_not_called()
    deal_agent.run.assert_called_once()


@pytest.mark.asyncio
async def test_casual_fallback_empty_falls_through_to_deal_agent() -> None:
    """Last-resort: if llama also returns empty, deal_agent still catches."""
    deal_agent = AsyncMock()
    deal_agent.run = AsyncMock(return_value="deal-agent reply")
    brain = _make_brain(deal_agent=deal_agent)

    with patch.object(brain, "_groq_with_tools", new=AsyncMock(return_value=("", None, None))), \
         patch.object(brain, "_casual_text_fallback", new=AsyncMock(return_value="")):

        out = await brain.respond("anything", user_id="u1", guild_id=10)

    # Empty casual → next safety net (deal_agent) runs.
    assert out == "deal-agent reply"
    deal_agent.run.assert_called_once()


# ---------------------------------------------------------------------------
# _casual_text_fallback unit behavior
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_casual_text_fallback_returns_model_text() -> None:
    brain = _make_brain()

    fake_response = type(
        "Resp", (), {
            "choices": [type(
                "C", (),
                {"message": type("M", (), {"content": "yeah I'd smash"})()})()
            ]
        }
    )()

    fake_client = type("Client", (), {})()
    fake_client.chat = type("Chat", (), {})()
    fake_client.chat.completions = type("Comp", (), {})()
    fake_client.chat.completions.create = AsyncMock(return_value=fake_response)

    messages = [{"role": "user", "content": "test"}]
    with patch("groq.AsyncGroq", return_value=fake_client):
        out = await brain._casual_text_fallback(messages, max_tok=110, guild_id=10)
    assert out == "yeah I'd smash"


@pytest.mark.asyncio
async def test_casual_text_fallback_strips_tool_call_markup() -> None:
    brain = _make_brain()

    fake_response = type(
        "Resp", (), {
            "choices": [type(
                "C", (),
                {"message": type("M", (), {
                    "content": "real text <function=music_assistant>{\"action\":\"play\"}</function> more"
                })()})()
            ]
        }
    )()

    fake_client = type("Client", (), {})()
    fake_client.chat = type("Chat", (), {})()
    fake_client.chat.completions = type("Comp", (), {})()
    fake_client.chat.completions.create = AsyncMock(return_value=fake_response)

    with patch("groq.AsyncGroq", return_value=fake_client):
        out = await brain._casual_text_fallback(
            [{"role": "user", "content": "x"}], max_tok=110, guild_id=10,
        )
    assert "<function=" not in out
    assert "music_assistant" not in out
    assert "real text" in out and "more" in out


@pytest.mark.asyncio
async def test_casual_text_fallback_handles_groq_exception() -> None:
    brain = _make_brain()

    fake_client = type("Client", (), {})()
    fake_client.chat = type("Chat", (), {})()
    fake_client.chat.completions = type("Comp", (), {})()
    fake_client.chat.completions.create = AsyncMock(
        side_effect=RuntimeError("groq down"),
    )

    with patch("groq.AsyncGroq", return_value=fake_client):
        out = await brain._casual_text_fallback(
            [{"role": "user", "content": "x"}], max_tok=110, guild_id=10,
        )
    assert out == ""


# ---------------------------------------------------------------------------
# Per-guild isolation of the new path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_casual_fallback_saves_to_correct_guild_history() -> None:
    brain = _make_brain(deal_agent=None)

    with patch.object(brain, "_groq_with_tools", new=AsyncMock(return_value=("", None, None))), \
         patch.object(brain, "_casual_text_fallback", new=AsyncMock(return_value="reply A")):

        await brain.respond("hi", user_id="u1", guild_id=10)

    with patch.object(brain, "_groq_with_tools", new=AsyncMock(return_value=("", None, None))), \
         patch.object(brain, "_casual_text_fallback", new=AsyncMock(return_value="reply B")):

        await brain.respond("hi", user_id="u1", guild_id=20)

    assert brain._histories[(10, "u1")][-1].content == "reply A"
    assert brain._histories[(20, "u1")][-1].content == "reply B"


# ---------------------------------------------------------------------------
# System prompt guarantees
# ---------------------------------------------------------------------------

def test_system_prompt_lists_banned_refusal_phrases() -> None:
    """The NEVER block enumerates literal RLHF refusal phrases. Tightening
    this list is part of the same fix (docs/decisions/
    text-casual-fallback-bypass-deal-agent.md). Regression guard."""
    from poob.brain.poob import _build_system_prompt
    prompt = _build_system_prompt(level=5, voice=False, with_tools=True)
    for phrase in (
        "as an AI",
        "I'm sorry but",
        "I cannot",
        "I can't comply",
        "I'm a large language model",
    ):
        assert phrase in prompt, f"missing banned phrase from NEVER list: {phrase!r}"
