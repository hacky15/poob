"""Tests for the voice hallucination-drop speaking a line instead of going silent.

Prod report (2026-07-09): user said "Hey, Poob" at the end of an unrelated
sentence (dual wake-gate correctly fired, addressed=True). The router
hallucinated a play query from stale context; the guard correctly caught and
dropped it (matches docs/gotchas/tool-hallucination-from-passive-context.md)
-- but voice then said NOTHING, which reads as "the bot ignored me" from a
confirmed address. See docs/decisions/voice-hallucination-drop-gets-a-line.md.

Fix: _handle_music_voice_streaming's hallucination-drop branch now streams a
short, in-character Toob reaction (_stream_toob_no_command_understood)
instead of returning silently. Scoped exactly to that branch -- the
re-derivation-succeeds path and the empty-query path are untouched.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import Mock, patch

import pytest

from poob.brain.poob import PoobBrain


def _make_brain() -> PoobBrain:
    brain = PoobBrain(groq_api_key="test-key", deal_agent=None)

    async def _music_handler(*args: Any, **kwargs: Any) -> str:
        return "Playing test track [3:00]"

    brain.set_music_handler(_music_handler)
    return brain


def _async_iter(items: list[str]):
    async def _gen():
        for i in items:
            yield i

    return _gen()


class _FakeStream:
    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration


# ---------------------------------------------------------------------------
# _handle_music_voice_streaming: hallucination-drop now speaks, doesn't
# silently return.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hallucination_drop_speaks_instead_of_silent_return() -> None:
    """The exact prod regression: current message has no play-intent, the
    hallucinated query has zero overlap, re-derivation also finds nothing --
    must yield the fallback reaction's sentences, not return empty."""
    brain = _make_brain()

    fallback = Mock(side_effect=lambda *a, **kw: _async_iter(["the hell did you even want"]))
    with patch.object(brain, "_stream_toob_no_command_understood", new=fallback):
        out = [
            s
            async for s in brain._handle_music_voice_streaming(
                "That's it? That's the song ends right there. Oh, wow. Hey, Poob.",
                "123",
                200,
                {"action": "play", "query": "homework drops as donut"},
                guild_id=10,
            )
        ]

    assert out == ["the hell did you even want"]
    fallback.assert_called_once()


@pytest.mark.asyncio
async def test_hallucination_drop_fallback_receives_the_raw_current_message() -> None:
    """The fallback reaction must react to what the user ACTUALLY said, not
    the hallucinated query -- passing the wrong string would make Toob react
    to gibberish instead of the user's real (if commandless) utterance."""
    brain = _make_brain()
    captured: dict[str, Any] = {}

    def _capture(*a: Any, **kw: Any):
        captured["args"] = a
        captured["kwargs"] = kw
        return _async_iter(["reaction"])

    with patch.object(brain, "_stream_toob_no_command_understood", new=_capture):
        async for _ in brain._handle_music_voice_streaming(
            "totally unrelated chatter, hey poob",
            "123",
            200,
            {"action": "play", "query": "stale hallucinated query"},
            guild_id=10,
        ):
            pass

    all_args = list(captured["args"]) + list(captured["kwargs"].values())
    assert "totally unrelated chatter, hey poob" in all_args
    assert "stale hallucinated query" not in all_args


@pytest.mark.asyncio
async def test_reextraction_success_path_unaffected_by_the_fallback() -> None:
    """When the raw message DOES carry genuine play-intent, re-derivation
    must still succeed and actually queue the song -- the fallback reaction
    must NOT fire in this path."""
    brain = _make_brain()
    handler_calls: list[Any] = []

    async def _handler(message, uid, gid, *, voice, tool_args):  # type: ignore[no-untyped-def]
        handler_calls.append(tool_args)
        return "Playing tiki tiki [3:00]"

    brain.set_music_handler(_handler)

    fallback = Mock(side_effect=lambda *a, **kw: _async_iter(["should NOT be reached"]))
    with patch.object(brain, "_stream_toob_no_command_understood", new=fallback):
        out = [
            s
            async for s in brain._handle_music_voice_streaming(
                "play tiki tiki",
                "123",
                200,
                {"action": "play", "query": "panda desiigner"},
                guild_id=10,
            )
        ]

    fallback.assert_not_called()
    assert not any("should NOT be reached" in s for s in out)


@pytest.mark.asyncio
async def test_empty_query_path_unaffected_by_the_fallback() -> None:
    """The 'play what?' empty-query prompt is a different branch entirely --
    the hallucination fallback must not fire there."""
    brain = _make_brain()

    fallback = Mock(side_effect=lambda *a, **kw: _async_iter(["should NOT be reached"]))
    with patch.object(brain, "_stream_toob_no_command_understood", new=fallback):
        out = [
            s
            async for s in brain._handle_music_voice_streaming(
                "Hey Poob play",
                "123",
                200,
                {"action": "play", "query": ""},
                guild_id=10,
            )
        ]

    fallback.assert_not_called()
    assert out == ["play what?"]


# ---------------------------------------------------------------------------
# _stream_toob_no_command_understood -- the reaction's own prompt contract.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_command_reaction_does_not_fabricate_a_request() -> None:
    """The prompt must tell the model there was NO request -- reusing
    _stream_toob_wrap_from_query's 'mock them for wanting it' framing would
    have Toob invent/react to a request that was never made."""
    brain = _make_brain()
    captured: dict[str, list] = {}

    async def _fake_create(**kwargs: Any) -> Any:
        captured["msgs"] = kwargs["messages"]
        return _FakeStream()

    fake_client = type("Client", (), {})()
    fake_client.chat = type("Chat", (), {})()
    fake_client.chat.completions = type("Comp", (), {})()
    fake_client.chat.completions.create = _fake_create

    with patch("groq.AsyncGroq", return_value=fake_client):
        async for _ in brain._stream_toob_no_command_understood(
            "hey poob",
            max_tokens=200,
        ):
            pass

    sys_prompt = captured["msgs"][0]["content"].lower()
    assert (
        "didn't actually ask" in sys_prompt or "no request" in sys_prompt or "nothing" in sys_prompt
    )
    assert "invent" in sys_prompt or "don't" in sys_prompt


@pytest.mark.asyncio
async def test_no_command_reaction_uses_same_tight_token_budget_as_toob_wrap() -> None:
    """One sentence, 6-10 words -- same discipline as the real-request wrap,
    matching Toob's established tight-line character."""
    brain = _make_brain()
    captured: dict[str, int] = {}

    async def _fake_create(**kwargs: Any) -> Any:
        captured["max_tokens"] = kwargs["max_tokens"]
        return _FakeStream()

    fake_client = type("Client", (), {})()
    fake_client.chat = type("Chat", (), {})()
    fake_client.chat.completions = type("Comp", (), {})()
    fake_client.chat.completions.create = _fake_create

    with patch("groq.AsyncGroq", return_value=fake_client):
        async for _ in brain._stream_toob_no_command_understood(
            "hey poob",
            max_tokens=500,
        ):
            pass

    assert captured["max_tokens"] <= 40


@pytest.mark.asyncio
async def test_no_command_reaction_falls_back_to_nothing_without_groq_key() -> None:
    """Consistent with every other Toob wrap in this file: no key, no crash,
    just no output."""
    brain = PoobBrain(groq_api_key="", deal_agent=None)

    out = [s async for s in brain._stream_toob_no_command_understood("hey poob", 200)]
    assert out == []
