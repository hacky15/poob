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
async def test_casual_empty_does_not_fall_to_deal_agent() -> None:
    """Deals must NEVER be the fallback. A 429/empty-routing on a casual
    message falls to ``_fallback_generate`` — the deal agent stays untouched.
    Operator directive: deals/marketplace only when deliberately requested.
    See docs/incidents/groq-429-fallback-routed-to-deals.md.
    """
    deal_agent = AsyncMock()
    deal_agent.run = AsyncMock(return_value="deal-agent reply")
    brain = _make_brain(deal_agent=deal_agent)

    with patch.object(brain, "_groq_with_tools", new=AsyncMock(return_value=("", None, None))), \
         patch.object(brain, "_casual_text_fallback", new=AsyncMock(return_value="")), \
         patch.object(brain, "_fallback_generate", new=AsyncMock(return_value="poob here augh")):

        out = await brain.respond("anything", user_id="u1", guild_id=10)

    assert out == "poob here augh"
    deal_agent.run.assert_not_called()


@pytest.mark.asyncio
async def test_groq_failure_casual_routes_casual_not_deal() -> None:
    """Groq raising (e.g. 429) on a casual message routes through the casual
    fallback, never the deal agent. See
    docs/incidents/groq-429-fallback-routed-to-deals.md.
    """
    deal_agent = AsyncMock()
    deal_agent.run = AsyncMock(return_value="DEAL")
    brain = _make_brain(deal_agent=deal_agent)

    with patch.object(brain, "_groq_with_tools", new=AsyncMock(side_effect=RuntimeError("429"))), \
         patch.object(brain, "_casual_text_fallback", new=AsyncMock(return_value="yo whats good")):

        out = await brain.respond("how are you", user_id="u1", guild_id=10)

    assert out == "yo whats good"
    deal_agent.run.assert_not_called()


@pytest.mark.asyncio
async def test_groq_failure_music_request_routes_music_not_deal() -> None:
    """Groq raising (e.g. 429) on a clear "play X" routes to music via the
    safety net — not the deal agent. The query is re-derived from the raw
    message so degraded routing can't lose it.
    See docs/incidents/groq-429-fallback-routed-to-deals.md.
    """
    deal_agent = AsyncMock()
    deal_agent.run = AsyncMock(return_value="DEAL")
    brain = _make_brain(deal_agent=deal_agent)
    brain._music_handler = object()  # non-None so the safety net runs

    with patch.object(brain, "_groq_with_tools", new=AsyncMock(side_effect=RuntimeError("429"))), \
         patch.object(brain, "_handle_music", new=AsyncMock(return_value="now playing tiki tiki")) as handle_music:

        out = await brain.respond("play tiki tiki", user_id="u1", guild_id=10)

    assert out == "now playing tiki tiki"
    handle_music.assert_called_once()
    deal_agent.run.assert_not_called()


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

def test_system_prompt_forbids_unsolicited_marketplace() -> None:
    """Operator directive: Poob must mention deals / marketplace / searching
    ONLY when explicitly asked — never volunteer it. The constraint must be
    present on every persona-prompt variant (casual + tool, voice + text),
    so it holds regardless of which path generates the reply.
    See docs/incidents/text-mode-rlhf-refusal-leak-2026-05-29.md (Fix 3)."""
    from poob.brain.poob import _build_system_prompt
    for voice in (True, False):
        for with_tools in (True, False):
            p = _build_system_prompt(level=5, voice=voice, with_tools=with_tools).lower()
            ctx = f"voice={voice} with_tools={with_tools}"
            assert "bring up" in p, f"missing don't-bring-up rule ({ctx})"
            assert "marketplace" in p, f"missing marketplace term ({ctx})"
            assert "unless" in p, f"missing 'unless they ask' qualifier ({ctx})"


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


# ---------------------------------------------------------------------------
# Music-routing prompt: thoughtful classification, no hardcoded play==music.
# See docs/decisions/music-routing-prompt-thoughtfulness.md.
# ---------------------------------------------------------------------------

def test_music_prompt_has_negative_examples() -> None:
    """The tool block must teach the model that 'play' isn't always music —
    negative examples prevent false positives like playing 'a game'.
    Only present on the with_tools variant (the routing call)."""
    from poob.brain.poob import _build_system_prompt
    p = _build_system_prompt(level=5, voice=False, with_tools=True).lower()
    assert "play a game" in p, "missing 'play a game' negative example"
    assert "good play" in p or "nice play" in p, "missing praise negative example"
    assert "isn't always" in p, "missing the 'play isn't always music' caveat"


def test_music_prompt_current_turn_only() -> None:
    """The tool block must instruct current-turn-only song extraction — the
    anti-hallucination guidance at the source. See
    docs/incidents/music-tool-hallucination.md."""
    from poob.brain.poob import _build_system_prompt
    p = _build_system_prompt(level=5, voice=False, with_tools=True).lower()
    assert "this message only" in p, "missing current-turn-only extraction rule"
    assert "earlier turns" in p or "earlier in the conversation" in p, (
        "missing 'never from earlier turns' rule"
    )


def test_music_prompt_nonsense_name_is_the_song() -> None:
    """A nonsense-sounding name after 'play' must still be treated as the song
    (the false-negative the operator hit: 'play tiki tiki' / 'cheeky cheeky')."""
    from poob.brain.poob import _build_system_prompt
    p = _build_system_prompt(level=5, voice=False, with_tools=True).lower()
    assert "nonsense" in p, "missing the nonsense-name-is-the-song guidance"
    assert "cheeky cheeky" in p or "tiki tiki" in p, "missing the nonsense example"


def test_music_tool_query_description_is_current_turn_only() -> None:
    """MUSIC_TOOL.query must say extract from the CURRENT message and never
    from earlier context — the tool-def half of the anti-hallucination fix."""
    from poob.brain.poob import MUSIC_TOOL
    desc = MUSIC_TOOL["function"]["parameters"]["properties"]["query"]["description"].lower()
    assert "current message" in desc, "query desc missing 'current message'"
    assert "never" in desc and "earlier" in desc, (
        "query desc missing 'never a title from earlier' rule"
    )


def test_music_prompt_drops_anything_music_adjacent_overreach() -> None:
    """The old 'if the user says ANYTHING that could be a request ... you MUST'
    wording compounded hallucination (docs/incidents/music-tool-hallucination.md)
    and was replaced with scoped guidance. Guard against its return."""
    from poob.brain.poob import _build_system_prompt
    p = _build_system_prompt(level=5, voice=False, with_tools=True).lower()
    assert "anything that could be" not in p, (
        "the over-aggressive 'ANYTHING that could be' wording regressed"
    )


# ---------------------------------------------------------------------------
# Hallucination guard: re-extract from the raw message, don't drop a clear
# "play X". See docs/gotchas/tool-hallucination-from-passive-context.md.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_hallucinated_query_reextracted_from_raw_message() -> None:
    """The LLM routes a play with a query lifted from stale context
    ("panda desiigner") while the user actually said "play tiki tiki".
    The guard must re-derive "tiki tiki" from the raw message and play THAT,
    not drop with "I didn't catch a music request there."
    """
    brain = _make_brain()
    captured: dict[str, Any] = {}

    async def _capture_handler(message, uid, gid, *, voice, tool_args):  # type: ignore[no-untyped-def]
        captured["tool_args"] = tool_args
        return "Playing tiki tiki [3:00]"

    brain.set_music_handler(_capture_handler)

    out = await brain._handle_music(
        "play tiki tiki", "123", voice=False, max_tok=200,
        tool_args={"action": "play", "query": "panda desiigner"}, guild_id=10,
    )

    assert captured["tool_args"]["query"] == "tiki tiki"
    assert "didn't catch" not in out.lower()
    assert "tiki tiki" in out


@pytest.mark.asyncio
async def test_reextracted_query_is_scrubbed_of_nested_verbs() -> None:
    """The safety net strips only ONE leading verb, so a doubled/nested form
    ("play play tiki") re-extracts to "play tiki". That must be scrubbed
    through _scrub_music_query before reaching the handler — ytdl must never
    get "play X". See docs/gotchas/tool-hallucination-from-passive-context.md.
    """
    brain = _make_brain()
    captured: dict[str, Any] = {}

    async def _capture_handler(message, uid, gid, *, voice, tool_args):  # type: ignore[no-untyped-def]
        captured["tool_args"] = tool_args
        return "Playing tiki [3:00]"

    brain.set_music_handler(_capture_handler)

    out = await brain._handle_music(
        "play play tiki", "123", voice=False, max_tok=200,
        tool_args={"action": "play", "query": "panda desiigner"}, guild_id=10,
    )

    assert captured["tool_args"]["query"] == "tiki"
    assert "didn't catch" not in out.lower()


@pytest.mark.asyncio
async def test_hallucinated_query_still_dropped_when_no_play_intent() -> None:
    """The guard must STILL drop when the raw message carries no play-intent —
    re-extraction is a backstop for clear "play X", not a way to play random
    context. "how's the weather" + a hallucinated play query → no music.
    """
    brain = _make_brain()
    called = {"hit": False}

    async def _handler(message, uid, gid, *, voice, tool_args):  # type: ignore[no-untyped-def]
        called["hit"] = True
        return "Playing something"

    brain.set_music_handler(_handler)

    out = await brain._handle_music(
        "how's the weather", "123", voice=False, max_tok=200,
        tool_args={"action": "play", "query": "panda desiigner"}, guild_id=10,
    )

    assert called["hit"] is False
    assert "didn't catch a music request" in out.lower()
