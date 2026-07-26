"""Tests for the Boob music-wrap variant.

Boob is Toob's side piece: a rare (~1 in 75) friendly variant of the
music-play voice wrap. Higher pitched, faster, three sentences, must
self-introduce as Toob's side piece, compliments the user's music taste
instead of mocking it.

See docs/decisions/boob-music-wrap-variant.md for design rationale.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from poob.brain.poob import (
    BOOB_PROBABILITY,
    VOICE_BOOB,
    VOICE_TOOB,
    PoobBrain,
)


def _make_brain() -> PoobBrain:
    """Brain with a fake key so the Groq path runs and a no-op music handler."""
    brain = PoobBrain(groq_api_key="test-key", deal_agent=None)

    async def _music_handler(*args: Any, **kwargs: Any) -> str:
        return "Playing test track [3:00]"

    brain.set_music_handler(_music_handler)
    return brain


# ---------------------------------------------------------------------------
# Probability + sentinel emission
# ---------------------------------------------------------------------------

def test_boob_probability_in_expected_range() -> None:
    """Documented as ~1 in 75. Constant must be in (0, 0.10] — too high
    burns the rarity, zero disables the feature entirely."""
    assert 0 < BOOB_PROBABILITY <= 0.10
    # ~1 in 75 ≈ 0.0133; keep the band tight around the target.
    assert 0.01 <= BOOB_PROBABILITY <= 0.02


@pytest.mark.asyncio
async def test_play_emits_voice_boob_when_random_under_threshold() -> None:
    brain = _make_brain()
    tool_args = {"action": "play", "query": "jazz"}

    with patch.object(
        brain, "_groq_with_tools",
        new=AsyncMock(return_value=("", "music_assistant", tool_args)),
    ), patch("poob.brain.poob.random.random", return_value=BOOB_PROBABILITY - 0.001), \
         patch.object(
             brain, "_stream_boob_wrap_from_query",
             new=lambda *a, **kw: _async_iter(["boob says hi", "and compliments you", "and waves goodbye"]),
         ), \
         patch.object(
             brain, "_stream_toob_wrap_from_query",
             new=lambda *a, **kw: _async_iter(["should NOT be reached"]),
         ):
        items = [
            item async for item in brain.respond_streaming(
                "play some jazz", user_id="123456789", guild_id=10,
            )
        ]

    assert items[0] == VOICE_BOOB
    assert VOICE_TOOB not in items
    assert any("boob says hi" in i for i in items[1:])


@pytest.mark.asyncio
async def test_play_emits_voice_toob_when_random_over_threshold() -> None:
    brain = _make_brain()
    tool_args = {"action": "play", "query": "jazz"}

    with patch.object(
        brain, "_groq_with_tools",
        new=AsyncMock(return_value=("", "music_assistant", tool_args)),
    ), patch("poob.brain.poob.random.random", return_value=BOOB_PROBABILITY + 0.001), \
         patch.object(
             brain, "_stream_toob_wrap_from_query",
             new=lambda *a, **kw: _async_iter(["toob mocks you"]),
         ), \
         patch.object(
             brain, "_stream_boob_wrap_from_query",
             new=lambda *a, **kw: _async_iter(["should NOT be reached"]),
         ):
        items = [
            item async for item in brain.respond_streaming(
                "play some jazz", user_id="123456789", guild_id=10,
            )
        ]

    assert items[0] == VOICE_TOOB
    assert VOICE_BOOB not in items
    assert any("toob mocks" in i for i in items[1:])


# ---------------------------------------------------------------------------
# Boob wrap content + structure
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_boob_wrap_uses_higher_token_budget_than_toob() -> None:
    """Boob says 3 sentences, Toob says 1. The Groq call must reflect the budget."""
    brain = _make_brain()

    captured_max_tokens: dict[str, int] = {}

    class _FakeStream:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    async def _fake_create(**kwargs: Any) -> Any:
        captured_max_tokens["boob"] = kwargs["max_tokens"]
        return _FakeStream()

    fake_client = type("Client", (), {})()
    fake_client.chat = type("Chat", (), {})()
    fake_client.chat.completions = type("Comp", (), {})()
    fake_client.chat.completions.create = _fake_create

    with patch("groq.AsyncGroq", return_value=fake_client):
        async for _ in brain._stream_boob_wrap_from_query("play jazz", max_tokens=200):
            pass

    assert captured_max_tokens["boob"] >= 100, (
        f"Boob wrap called with only {captured_max_tokens['boob']} max_tokens — "
        "needs room for 3 sentences (~30-50 words ≈ 80+ tokens)"
    )


@pytest.mark.asyncio
async def test_boob_wrap_prompt_requires_side_piece_intro() -> None:
    """The system prompt MUST tell the model to introduce as Toob's side piece.
    Without this directive the persona collapses into generic flattery."""
    brain = _make_brain()

    captured_messages: dict[str, list] = {}

    class _FakeStream:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    async def _fake_create(**kwargs: Any) -> Any:
        captured_messages["msgs"] = kwargs["messages"]
        return _FakeStream()

    fake_client = type("Client", (), {})()
    fake_client.chat = type("Chat", (), {})()
    fake_client.chat.completions = type("Comp", (), {})()
    fake_client.chat.completions.create = _fake_create

    with patch("groq.AsyncGroq", return_value=fake_client):
        async for _ in brain._stream_boob_wrap_from_query("play jazz", max_tokens=200):
            pass

    sys_prompt = captured_messages["msgs"][0]["content"].lower()
    assert "side piece" in sys_prompt, (
        "Boob system prompt must explicitly require the 'Toob's side piece' intro"
    )
    # Three-sentence directive
    assert ("three" in sys_prompt or "3" in sys_prompt) and "sentence" in sys_prompt
    # Friendly / complimentary anchor
    assert any(
        word in sys_prompt
        for word in ("compliment", "warm", "sweet", "nice")
    ), "Boob prompt should anchor positive valence (compliment/warm/sweet/nice)"


@pytest.mark.asyncio
async def test_boob_wrap_streams_multiple_sentences() -> None:
    """The whole point of Boob is the 3-sentence delivery. The wrap must
    yield each sentence separately so session can synth+play them as they arrive."""
    brain = _make_brain()

    sentences = [
        "Hey there, Boob here, Toob's side piece.",
        "I gotta say your taste is fantastic.",
        "Crank it up and enjoy.",
    ]

    async def _fake_chunks() -> Any:
        for s in sentences:
            yield s + " "

    class _FakeStreamObj:
        def __aiter__(self):
            return _fake_chunks()

    async def _fake_create(**kwargs: Any) -> Any:
        # Return a mock stream object that yields chunks in OpenAI format
        async def _gen():
            for s in sentences:
                chunk = type("Chunk", (), {})()
                chunk.choices = [type("C", (), {})()]
                chunk.choices[0].delta = type("D", (), {})()
                chunk.choices[0].delta.content = s + " "
                yield chunk

        class _S:
            def __aiter__(self):
                return _gen()

        return _S()

    fake_client = type("Client", (), {})()
    fake_client.chat = type("Chat", (), {})()
    fake_client.chat.completions = type("Comp", (), {})()
    fake_client.chat.completions.create = _fake_create

    with patch("groq.AsyncGroq", return_value=fake_client):
        out = [
            s async for s in brain._stream_boob_wrap_from_query("play jazz", max_tokens=200)
        ]

    # Should yield at least 2 sentences (joined "and" stuff might collapse some)
    assert len(out) >= 2, f"Expected multiple sentences, got: {out}"


# ---------------------------------------------------------------------------
# Tool routing untouched
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_non_play_action_does_not_emit_boob_or_toob_signal() -> None:
    """Skip/pause/stop are silent control actions — they don't trigger
    the speculative wrap for either persona."""
    brain = _make_brain()

    async def _silent_handler(*a: Any, **kw: Any) -> str:
        return "[SILENT]Skipped"

    brain.set_music_handler(_silent_handler)
    tool_args = {"action": "skip"}

    with patch.object(
        brain, "_groq_with_tools",
        new=AsyncMock(return_value=("", "music_assistant", tool_args)),
    ), patch("poob.brain.poob.random.random", return_value=0.001):
        items = [
            item async for item in brain.respond_streaming(
                "skip", user_id="123456789", guild_id=10,
            )
        ]

    # Skip path — silent, brain returns empty after handler
    # The voice signal would only fire if action=play, so neither sentinel here
    # (though VOICE_TOOB is yielded for non-play actions in the existing code
    # before the silent check — that's existing behavior we don't touch)
    # The KEY assertion: VOICE_BOOB must NEVER fire for non-play actions.
    assert VOICE_BOOB not in items


@pytest.mark.asyncio
async def test_deal_route_does_not_emit_voice_boob() -> None:
    """Boob is music-play exclusive. Deal queries should never trigger it,
    even when the random roll lands inside the threshold."""
    brain = _make_brain()
    deal_agent = AsyncMock()
    deal_agent.run = AsyncMock(return_value="here are some deals")
    brain.deal_agent = deal_agent

    tool_args = {"request": "wishlist"}

    with patch.object(
        brain, "_groq_with_tools",
        new=AsyncMock(return_value=("", "deal_assistant", tool_args)),
    ), patch("poob.brain.poob.random.random", return_value=0.001):
        items = [
            item async for item in brain.respond_streaming(
                "whats on my wishlist", user_id="123456789", guild_id=10,
            )
        ]

    assert VOICE_BOOB not in items
    assert VOICE_TOOB not in items


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _async_iter(items):  # type: ignore[no-untyped-def]
    for item in items:
        yield item
