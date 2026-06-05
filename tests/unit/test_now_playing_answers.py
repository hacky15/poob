"""Poob answers questions about the currently-playing song conversationally.

The voice/music layers inject the live current track into the brain's prompt
(refreshed per-utterance, so it's fresh even across auto-advance). Questions
like "what's this song / who sings it / how long is it" must be answered from
that context directly — no tool call, no search ("just has the info, not
overloaded"). Control commands (skip/pause/volume) still route to the music
tool. See docs/decisions/now-playing-conversational-answers.md.
"""

from __future__ import annotations

from poob.brain.poob import PoobBrain


def test_info_questions_answered_conversationally() -> None:
    block = PoobBrain._music_context_block("Desiigner - Panda [4:03]").lower()
    # The facts Poob needs are present on the line...
    assert "desiigner - panda [4:03]" in block
    assert "artist - song" in block  # so "who sings this" is derivable from the title
    # ...and info questions are explicitly directed to a SPOKEN answer, no tool.
    assert "who sings" in block or "performs it" in block
    assert "how long" in block or "length" in block
    assert "do not call a tool" in block
    assert "not search" in block


def test_control_still_routes_to_tool() -> None:
    block = PoobBrain._music_context_block("X - Y [1:00]").lower()
    assert "music_assistant" in block
    assert "must call" in block
    for verb in ("skip", "pause", "resume", "stop", "shuffle", "loop"):
        assert verb in block


def test_block_carries_the_live_track_string() -> None:
    # Whatever the music layer set (title + duration) is surfaced verbatim.
    block = PoobBrain._music_context_block("IShowSpeed - World Cup [4:58]")
    assert "IShowSpeed - World Cup [4:58]" in block


def test_build_messages_injects_info_guidance_when_playing() -> None:
    brain = PoobBrain(groq_api_key="", deal_agent=None)
    brain._set_music_playing_info(10, "Madison Beer - Complexity [2:38]")
    msgs = brain._build_messages("u", "who sings this", voice=True, guild_id=10)
    sys = next(m for m in msgs if m["role"] == "system")["content"].lower()
    assert "madison beer - complexity [2:38]" in sys
    assert "who sings" in sys or "performs it" in sys
    assert "do not call a tool" in sys


def test_no_music_block_when_nothing_playing() -> None:
    brain = PoobBrain(groq_api_key="", deal_agent=None)
    msgs = brain._build_messages("u", "hey", voice=True, guild_id=99)
    sys = next(m for m in msgs if m["role"] == "system")["content"]
    assert "MUSIC IS CURRENTLY PLAYING" not in sys
