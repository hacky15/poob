"""Tests for the text-channel music response wrap.

Prod report (2026-07-03): "@Poob play gobble glitch remix 808 backwoods" (a
plain text play request) got wrapped into "Sure thing—no items, no prices, no
questions, no confirmations. Just the song's name, length, and the fact it's
playing." — a bizarre, off-topic, oversized reply to a one-line status.

Root cause: the text-channel short-response path in `_handle_music` reused
`_wrap_in_personality`, which is built for DEAL responses — its assistant-turn
label ("[My deal system says: ...]") and instruction ("include items, prices,
questions asked, confirmations") are deal-shaped. A music result has none of
those things, so the model narrated their ABSENCE instead of just relaying the
song. Fixed with a dedicated `_wrap_music_response_text` (the text-channel
counterpart to the existing voice-only `_wrap_music_response` / Toob wrap),
with a correctly-labeled, music-only instruction and a tight token cap.
`_wrap_in_personality` itself is UNCHANGED and still exclusively serves
`_handle_deal` — zero risk to deal-response wrapping.

Adversarial re-review of the fix (same day) caught two further issues, both
fixed here too: `_wrap_music_response_text` was pulling the guild's ROLLED
VOICE horniness level into a text reply (removed — text is always neutral,
level pinned to 5, and the now-unused `guild_id` param was dropped); and its
token cap (60) was looser than the analogous Toob wrap (40) despite this
function's whole purpose being to prevent a one-line status from ballooning —
tightened to match.
"""

from __future__ import annotations

import inspect
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

import poob.brain.poob as poob_module
from poob.brain.poob import PoobBrain


def _make_brain(**kw: Any) -> PoobBrain:
    return PoobBrain(groq_api_key="test-key", deal_agent=None, **kw)


def _fake_groq_client(content: str | None) -> Any:
    """Mimic the Groq SDK response shape used elsewhere in this test suite."""
    fake_response = type(
        "Resp",
        (),
        {"choices": [type("C", (), {"message": type("M", (), {"content": content})()})()]},
    )()
    client = type("Client", (), {})()
    client.chat = type("Chat", (), {})()
    client.chat.completions = type("Comp", (), {})()
    client.chat.completions.create = AsyncMock(return_value=fake_response)
    return client


# ---------------------------------------------------------------------------
# _handle_music dispatches text (non-voice) short responses to the NEW
# music-specific wrap, not the deal-shaped _wrap_in_personality.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_text_music_response_uses_music_wrap_not_deal_wrap() -> None:
    brain = _make_brain()

    async def _handler(message, uid, gid, *, voice, tool_args):  # type: ignore[no-untyped-def]
        return "Playing gobble glitch remix 808 backwoods [3:42]"

    brain.set_music_handler(_handler)

    with (
        patch.object(
            brain, "_wrap_music_response_text", new=AsyncMock(return_value="wrapped!")
        ) as music_wrap,
        patch.object(brain, "_wrap_in_personality", new=AsyncMock()) as deal_wrap,
    ):
        out = await brain._handle_music(
            "play gobble glitch remix 808 backwoods",
            "123",
            voice=False,
            max_tok=200,
            tool_args={"action": "play", "query": "gobble glitch remix 808 backwoods"},
            guild_id=10,
        )

    # Exact-args check, not just "was called" — catches argument-order or
    # omission regressions at the call site, not just the wrong method firing.
    music_wrap.assert_awaited_once_with(
        "play gobble glitch remix 808 backwoods",
        "Playing gobble glitch remix 808 backwoods [3:42]",
        200,
    )
    deal_wrap.assert_not_called()
    assert out == "wrapped!"


# ---------------------------------------------------------------------------
# _wrap_music_response_text — the actual prompt contract.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_music_text_wrap_label_is_music_not_deal() -> None:
    """The exact regression: the assistant-turn label must say 'Music system
    result', never 'My deal system says' (which caused the model to treat a
    song status as if it were a deal listing)."""
    brain = _make_brain()
    client = _fake_groq_client("Bumping gobble glitch remix now.")

    with patch("groq.AsyncGroq", return_value=client):
        await brain._wrap_music_response_text(
            "play gobble glitch remix 808 backwoods",
            "Playing gobble glitch remix 808 backwoods [3:42]",
            200,
        )

    sent_messages = client.chat.completions.create.call_args.kwargs["messages"]
    labels = [m["content"] for m in sent_messages if m["role"] == "assistant"]
    assert any("Music system result" in c for c in labels)
    assert not any("deal system" in c.lower() for c in labels)


@pytest.mark.asyncio
async def test_music_text_wrap_instruction_has_no_deal_vocabulary() -> None:
    """The instruction line must never mention items/prices/questions/
    confirmations — those categories don't exist for a music result, and
    listing them is exactly what caused the model to narrate their absence."""
    brain = _make_brain()
    client = _fake_groq_client("Bumping it now.")

    with patch("groq.AsyncGroq", return_value=client):
        await brain._wrap_music_response_text(
            "play tiki tiki",
            "Playing tiki tiki [3:00]",
            200,
        )

    sent_messages = client.chat.completions.create.call_args.kwargs["messages"]
    final_instruction = sent_messages[-1]["content"].lower()
    for banned in ("item", "price", "question", "confirmation"):
        assert banned not in final_instruction, f"deal-shaped word leaked back in: {banned!r}"


@pytest.mark.asyncio
async def test_music_text_wrap_uses_poob_persona_at_neutral_level() -> None:
    """Text stays Poob's own persona at the neutral level (5) — NOT the
    guild's rolled voice-session horniness level, and not Toob (a voice-only
    pitch/timbre swap with no meaning in a text reply).

    Spies on the actual `_build_system_prompt` call rather than checking for
    absent strings: a bare string-absence check would have passed against the
    old buggy code too (it also called voice=False), so it wouldn't have
    caught the horniness-level regression this test guards against.
    """
    brain = _make_brain()
    # Simulate an elevated ROLLED VOICE horniness level for this guild — the
    # text wrap must NOT pick this up.
    brain._horniness_levels[10] = 9
    client = _fake_groq_client("Bumping it now.")

    with (
        patch("groq.AsyncGroq", return_value=client),
        patch(
            "poob.brain.poob._build_system_prompt",
            wraps=poob_module._build_system_prompt,
        ) as spy,
    ):
        await brain._wrap_music_response_text(
            "play tiki tiki",
            "Playing tiki tiki [3:00]",
            200,
        )

    spy.assert_called_once_with(5, voice=False)  # neutral level, not the rolled 9


@pytest.mark.asyncio
async def test_music_text_wrap_caps_tokens_tightly() -> None:
    """A one-line status has no business ballooning into a paragraph — capped
    to match the equally-tight Toob voice wrap (min(max_tokens, 40))."""
    brain = _make_brain()
    client = _fake_groq_client("Bumping it now.")

    with patch("groq.AsyncGroq", return_value=client):
        await brain._wrap_music_response_text(
            "play tiki tiki",
            "Playing tiki tiki [3:00]",
            500,
        )

    assert client.chat.completions.create.call_args.kwargs["max_tokens"] <= 40


@pytest.mark.asyncio
async def test_music_text_wrap_returns_model_text() -> None:
    brain = _make_brain()
    client = _fake_groq_client("Solid pick, bumping it.")

    with patch("groq.AsyncGroq", return_value=client):
        out = await brain._wrap_music_response_text(
            "play tiki tiki",
            "Playing tiki tiki [3:00]",
            200,
        )

    assert out == "Solid pick, bumping it."


@pytest.mark.asyncio
async def test_music_text_wrap_falls_back_to_raw_result_on_exception() -> None:
    brain = _make_brain()
    client = type("Client", (), {})()
    client.chat = type("Chat", (), {})()
    client.chat.completions = type("Comp", (), {})()
    client.chat.completions.create = AsyncMock(side_effect=RuntimeError("groq down"))

    with patch("groq.AsyncGroq", return_value=client):
        out = await brain._wrap_music_response_text(
            "play tiki tiki",
            "Playing tiki tiki [3:00]",
            200,
        )

    assert out == "Playing tiki tiki [3:00]"


@pytest.mark.asyncio
async def test_music_text_wrap_falls_back_when_no_groq_key() -> None:
    brain = PoobBrain(groq_api_key="", deal_agent=None)
    out = await brain._wrap_music_response_text(
        "play tiki tiki",
        "Playing tiki tiki [3:00]",
        200,
    )
    assert out == "Playing tiki tiki [3:00]"


# ---------------------------------------------------------------------------
# Regression guard: _wrap_in_personality (the deal wrap) is UNTOUCHED and
# still the sole wrap for _handle_deal. Zero risk to deal functionality.
#
# The structural check (below) only proves two substrings survive and the
# right method names are called — it can't catch a behavioral regression
# (wrong model/max_tokens/temperature/voice-branch). The behavioral tests
# above it actually exercise _wrap_in_personality's real Groq call for both
# voice=True and voice=False, closing that gap.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deal_wrap_behavior_text_mode() -> None:
    brain = _make_brain()
    client = _fake_groq_client("Found you a deal.")

    with patch("groq.AsyncGroq", return_value=client):
        out = await brain._wrap_in_personality(
            "any deals nearby?",
            "GOOD deal: $50 desk, listed $80",
            voice=False,
            max_tokens=200,
            guild_id=10,
        )

    kwargs = client.chat.completions.create.call_args.kwargs
    assert kwargs["model"] == brain.groq_model
    assert kwargs["max_tokens"] == 200
    assert kwargs["temperature"] == 0.8
    sent_messages = kwargs["messages"]
    labels = [m["content"] for m in sent_messages if m["role"] == "assistant"]
    assert any("My deal system says" in c for c in labels)
    final_instruction = sent_messages[-1]["content"]
    assert "items, prices, questions asked, confirmations" in final_instruction
    assert "VOICE MODE" not in final_instruction  # text mode omits the voice suffix
    assert out == "Found you a deal."


@pytest.mark.asyncio
async def test_deal_wrap_behavior_voice_mode_adds_voice_suffix() -> None:
    brain = _make_brain()
    client = _fake_groq_client("Found you a deal.")

    with patch("groq.AsyncGroq", return_value=client):
        await brain._wrap_in_personality(
            "any deals nearby?",
            "GOOD deal: $50 desk, listed $80",
            voice=True,
            max_tokens=110,
            guild_id=10,
        )

    kwargs = client.chat.completions.create.call_args.kwargs
    final_instruction = kwargs["messages"][-1]["content"]
    assert "VOICE MODE: spoken aloud, no caps, no formatting." in final_instruction


@pytest.mark.asyncio
async def test_deal_wrap_falls_back_to_raw_result_on_exception() -> None:
    brain = _make_brain()
    client = type("Client", (), {})()
    client.chat = type("Chat", (), {})()
    client.chat.completions = type("Comp", (), {})()
    client.chat.completions.create = AsyncMock(side_effect=RuntimeError("groq down"))

    with patch("groq.AsyncGroq", return_value=client):
        out = await brain._wrap_in_personality(
            "any deals?",
            "GOOD deal: $50 desk",
            voice=False,
            max_tokens=200,
            guild_id=10,
        )

    assert out == "GOOD deal: $50 desk"


def test_wrap_in_personality_structurally_unchanged() -> None:
    """Cheap structural guard (source-string check) as a first line of
    defense; the behavioral tests above are the real regression guard."""
    src = inspect.getsource(poob_module.PoobBrain._wrap_in_personality)
    assert "My deal system says" in src
    assert "items, prices, questions asked, confirmations" in src

    handle_deal_src = inspect.getsource(poob_module.PoobBrain._handle_deal)
    assert "_wrap_in_personality(" in handle_deal_src

    handle_music_src = inspect.getsource(poob_module.PoobBrain._handle_music)
    assert "_wrap_in_personality(" not in handle_music_src
    assert "_wrap_music_response_text(" in handle_music_src
