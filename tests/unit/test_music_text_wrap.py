"""Tests for the text-channel music response wrap.

History of this contract (each stage pinned here at the time):

1. 2026-07-03: text music replies reused the DEAL wrap and narrated absent
   deal categories ("no items, no prices…"). Fixed with a dedicated
   Poob-personality text wrap (music-only instruction, tight token cap).
2. 2026-07-12: operator decision — music replies are TOOB's everywhere, not
   just in voice ("it's supposed to just be toob. toob can say it in chat").
   The text path now reuses ``_wrap_music_response`` (the SAME Toob wrap the
   voice path uses) and returns it prefixed with the ``VOICE_TOOB`` sentinel
   so the agent handler strips it from the posted text and speaks the reply
   in Toob's voice when the requester shares the VC. The dedicated Poob text
   wrap was deleted. See decisions/music-text-replies-are-toob (supersedes
   the "text stays Poob" convention) and
   incidents/music-text-wrap-inherited-deal-instructions (stage 1).

The anti-deal-vocabulary guards from stage 1 remain — the Toob wrap must
never regress into deal-shaped instructions either.
"""

from __future__ import annotations

import inspect
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import poob.brain.poob as poob_module
from poob.brain.poob import VOICE_TOOB, PoobBrain
from poob.discord_bot.agent_handler import _split_persona


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
# _handle_music dispatches text (non-voice) short responses to the TOOB wrap
# and tags the result with the VOICE_TOOB sentinel.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_text_music_response_uses_toob_wrap_with_sentinel() -> None:
    brain = _make_brain()

    async def _handler(message, uid, gid, *, voice, tool_args):  # type: ignore[no-untyped-def]
        return "Playing gobble glitch remix 808 backwoods [3:42]"

    brain.set_music_handler(_handler)

    with (
        patch.object(
            brain, "_wrap_music_response", new=AsyncMock(return_value="your taste disgusts me")
        ) as toob_wrap,
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

    toob_wrap.assert_awaited_once_with(
        "play gobble glitch remix 808 backwoods",
        "Playing gobble glitch remix 808 backwoods [3:42]",
        200,
    )
    deal_wrap.assert_not_called()
    # Sentinel-prefixed so the agent handler can strip it and pick Toob's voice.
    assert out == VOICE_TOOB + "your taste disgusts me"


@pytest.mark.asyncio
async def test_text_music_history_saves_clean_text_not_sentinel() -> None:
    """The sentinel is transport, not content — conversation history must
    store the clean Toob line, or later persona calls see the marker."""
    brain = _make_brain()

    async def _handler(message, uid, gid, *, voice, tool_args):  # type: ignore[no-untyped-def]
        return "Playing tiki tiki [3:00]"

    brain.set_music_handler(_handler)

    with patch.object(
        brain, "_wrap_music_response", new=AsyncMock(return_value="suffer through it")
    ):
        await brain._handle_music(
            "play tiki tiki",
            "123",
            voice=False,
            max_tok=200,
            tool_args={"action": "play", "query": "tiki tiki"},
            guild_id=10,
        )

    history = brain._histories[(10, "123")]
    assert any(m.content == "suffer through it" for m in history)
    assert not any(VOICE_TOOB in m.content for m in history)


@pytest.mark.asyncio
async def test_silent_control_acks_are_not_toob_tagged() -> None:
    """Control acks (skip/pause/volume) stay persona-neutral — in voice they
    are never spoken at all; in text they post as plain status with no
    sentinel, so the VC speak (if any) stays Poob."""
    brain = _make_brain()

    async def _handler(message, uid, gid, *, voice, tool_args):  # type: ignore[no-untyped-def]
        return "[SILENT]Skipped Old Song."

    brain.set_music_handler(_handler)

    out = await brain._handle_music(
        "skip",
        "123",
        voice=False,
        max_tok=200,
        tool_args={"action": "skip"},
        guild_id=10,
    )

    assert out == "Skipped Old Song."
    assert VOICE_TOOB not in out


@pytest.mark.asyncio
async def test_speak_responses_toob_tagged_in_text_but_not_voice() -> None:
    """[SPEAK] verbatim answers (list_effects) are spoken by Toob in voice
    mode via the STREAMED sentinel — so the text return must carry the
    embedded sentinel ONLY in text mode. Embedding it for voice would leak
    the marker into TTS."""
    brain = _make_brain()

    async def _handler(message, uid, gid, *, voice, tool_args):  # type: ignore[no-untyped-def]
        return "[SPEAK]I've got 15 effects: nightcore, slowed..."

    brain.set_music_handler(_handler)

    text_out = await brain._handle_music(
        "what effects do you have",
        "123",
        voice=False,
        max_tok=200,
        tool_args={"action": "list_effects"},
        guild_id=10,
    )
    voice_out = await brain._handle_music(
        "what effects do you have",
        "123",
        voice=True,
        max_tok=200,
        tool_args={"action": "list_effects"},
        guild_id=10,
    )

    assert text_out.startswith(VOICE_TOOB)
    assert not voice_out.startswith(VOICE_TOOB)
    assert "15 effects" in text_out and "15 effects" in voice_out


# ---------------------------------------------------------------------------
# _wrap_music_response — the shared Toob prompt contract (voice AND text).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_music_wrap_label_is_music_not_deal() -> None:
    """The 2026-07-03 regression guard, carried forward: the assistant-turn
    label must say 'Music system result', never 'My deal system says'."""
    brain = _make_brain()
    client = _fake_groq_client("your lake vibes will drown you")

    with patch("groq.AsyncGroq", return_value=client):
        await brain._wrap_music_response(
            "play backwoods 808 fishing",
            "Playing Draggin Bottom [3:43]",
            200,
        )

    sent_messages = client.chat.completions.create.call_args.kwargs["messages"]
    labels = [m["content"] for m in sent_messages if m["role"] == "assistant"]
    assert any("Music system result" in c for c in labels)
    assert not any("deal system" in c.lower() for c in labels)


@pytest.mark.asyncio
async def test_music_wrap_instruction_has_no_deal_vocabulary() -> None:
    """Deal-shaped categories (items/prices/questions/confirmations) must
    never appear — listing them made the model narrate their absence."""
    brain = _make_brain()
    client = _fake_groq_client("mock line")

    with patch("groq.AsyncGroq", return_value=client):
        await brain._wrap_music_response(
            "play tiki tiki",
            "Playing tiki tiki [3:00]",
            200,
        )

    sent_messages = client.chat.completions.create.call_args.kwargs["messages"]
    final_instruction = sent_messages[-1]["content"].lower()
    for banned in ("item", "price", "question", "confirmation"):
        assert banned not in final_instruction, f"deal-shaped word leaked back in: {banned!r}"


@pytest.mark.asyncio
async def test_music_wrap_is_toob_persona() -> None:
    """The system prompt is TOOB's — dark music spirit, one venomous
    sentence — for text and voice alike."""
    brain = _make_brain()
    client = _fake_groq_client("mock line")

    with patch("groq.AsyncGroq", return_value=client):
        await brain._wrap_music_response(
            "play tiki tiki",
            "Playing tiki tiki [3:00]",
            200,
        )

    sent_messages = client.chat.completions.create.call_args.kwargs["messages"]
    system = next(m["content"] for m in sent_messages if m["role"] == "system")
    assert "Toob" in system
    assert "ONE sentence" in system


@pytest.mark.asyncio
async def test_music_wrap_caps_tokens_tightly() -> None:
    """A one-line status has no business ballooning into a paragraph.

    2026-08-26: cap raised 40 -> 100 when voice_llm_model became a
    reasoning model (gpt-oss-20b) — it spends part of the budget on hidden
    reasoning before any visible text, so 40 was no longer enough headroom
    to reliably produce output. The VISIBLE reply length is still governed
    by the prompt's "ONE sentence (6-10 words)" instruction, not this cap;
    this test still guards against unbounded ballooning, just at the new
    ceiling. See docs/incidents/voice-llm-model-deprecated-and-never-wired.md.
    """
    brain = _make_brain()
    client = _fake_groq_client("mock line")

    with patch("groq.AsyncGroq", return_value=client):
        await brain._wrap_music_response(
            "play tiki tiki",
            "Playing tiki tiki [3:00]",
            500,
        )

    assert client.chat.completions.create.call_args.kwargs["max_tokens"] <= 100


@pytest.mark.asyncio
async def test_music_wrap_falls_back_to_raw_result_on_exception() -> None:
    brain = _make_brain()
    client = type("Client", (), {})()
    client.chat = type("Chat", (), {})()
    client.chat.completions = type("Comp", (), {})()
    client.chat.completions.create = AsyncMock(side_effect=RuntimeError("groq down"))

    with patch("groq.AsyncGroq", return_value=client):
        out = await brain._wrap_music_response(
            "play tiki tiki",
            "Playing tiki tiki [3:00]",
            200,
        )

    assert out == "Playing tiki tiki [3:00]"


@pytest.mark.asyncio
async def test_music_wrap_falls_back_when_no_groq_key() -> None:
    brain = PoobBrain(groq_api_key="", deal_agent=None)
    out = await brain._wrap_music_response(
        "play tiki tiki",
        "Playing tiki tiki [3:00]",
        200,
    )
    assert out == "Playing tiki tiki [3:00]"


# ---------------------------------------------------------------------------
# _split_persona — the agent handler's sentinel → persona translation.
# ---------------------------------------------------------------------------


def test_split_persona_strips_toob_sentinel() -> None:
    persona, text = _split_persona(VOICE_TOOB + "your taste disgusts me")
    assert persona == "toob"
    assert text == "your taste disgusts me"
    assert VOICE_TOOB not in text


def test_split_persona_defaults_to_poob() -> None:
    persona, text = _split_persona("just a normal chat reply")
    assert persona == "poob"
    assert text == "just a normal chat reply"


def test_split_persona_sentinel_mid_string_is_not_a_tag() -> None:
    """Only a PREFIX is transport — a sentinel-looking string inside content
    (however unlikely) is left alone rather than mangled."""
    msg = f"someone typed {VOICE_TOOB} in chat"
    persona, text = _split_persona(msg)
    assert persona == "poob"
    assert text == msg


# ---------------------------------------------------------------------------
# speak_if_in_channel — persona-keyed synth dispatch.
# ---------------------------------------------------------------------------


def _speaking_fixture():
    """(fake VoiceCog self, message, session) wired so the VC gate passes."""
    from poob.discord_bot.cogs.voice_cog import VoiceCog

    session = MagicMock()
    session._synthesize = AsyncMock(return_value=b"poob-audio")
    session._synthesize_toob = AsyncMock(return_value=b"toob-audio")
    session._synthesize_boob = AsyncMock(return_value=b"boob-audio")
    session._play_audio = AsyncMock()

    fake_self = MagicMock()
    fake_self._get_session.return_value = session

    channel = MagicMock()
    session.voice_client.channel = channel
    message = MagicMock()
    message.guild = MagicMock()
    message.author.voice.channel = channel

    return VoiceCog.speak_if_in_channel, fake_self, message, session


@pytest.mark.asyncio
async def test_speak_if_in_channel_toob_persona_uses_toob_synth() -> None:
    speak, fake_self, message, session = _speaking_fixture()

    ok = await speak(fake_self, message, "suffer through this song", persona="toob")

    assert ok is True
    session._synthesize_toob.assert_awaited_once_with("suffer through this song")
    session._synthesize.assert_not_awaited()
    session._play_audio.assert_awaited_once_with(b"toob-audio")


@pytest.mark.asyncio
async def test_speak_if_in_channel_defaults_to_poob_synth() -> None:
    speak, fake_self, message, session = _speaking_fixture()

    ok = await speak(fake_self, message, "hey what's up")

    assert ok is True
    session._synthesize.assert_awaited_once_with("hey what's up")
    session._synthesize_toob.assert_not_awaited()


@pytest.mark.asyncio
async def test_speak_if_in_channel_unknown_persona_falls_back_to_poob() -> None:
    speak, fake_self, message, session = _speaking_fixture()

    ok = await speak(fake_self, message, "hello", persona="gloob")

    assert ok is True
    session._synthesize.assert_awaited_once()


# ---------------------------------------------------------------------------
# Regression guard: _wrap_in_personality (the deal wrap) is UNTOUCHED and
# still the sole wrap for _handle_deal. Zero risk to deal functionality.
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
    assert "_wrap_music_response(" in handle_music_src
    # The dedicated Poob text wrap is GONE — text reuses the Toob wrap.
    assert not hasattr(poob_module.PoobBrain, "_wrap_music_response_text")
