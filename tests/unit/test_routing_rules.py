"""Characterization tests for routing rules the slim-routing-prompt audit
found UNTESTED (docs/plans/slim-routing-prompt.md).

These pin the CURRENT behavior of the code paths that participate in the
tool-routing decision but had no regression coverage:

- ``_music_safety_net`` — deterministic play-intent override (a second
  tool-decision path, call sites poob.py:917/967).
- ``_scrub_music_query`` — verb stripping at the brain->handler boundary.
- the empty/short play-query gate in ``_handle_music`` (STT cutoff guard).
- the ``_TOOL_DETECTION_MAX_TOKENS`` floor that prevents gpt-oss-20b from
  truncating mid-arguments into a Groq ``tool_use_failed`` 400.
- the DEAL side of the ``looks_tool_worthy`` cascade-continuation gate (the
  music side was covered by test_brain_gemini_router, the deal side was not).
- the ACTIVE DEAL SESSION hint and the music-context block string contract
  that ``_build_messages`` injects into the routing prompt.

Written BEFORE the slim-routing-prompt change so any regression in these
rules is caught regardless of the prompt refactor.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from poob.brain.poob import DEAL_TOOL, PoobBrain


def _brain(**kw: Any) -> PoobBrain:
    return PoobBrain(deal_agent=None, **kw)


# --- _music_safety_net: the deterministic play-intent backstop --------------


def test_music_safety_net_forces_tool_on_clear_play_intent() -> None:
    """Clear "play X" / "put on X" / "queue X" phrasing the LLM missed is
    forced to music_assistant(play) with a best-effort extracted query."""
    b = _brain()
    assert b._music_safety_net("play tiki tiki", None, None) == (
        "music_assistant",
        {"action": "play", "query": "tiki tiki"},
    )
    assert b._music_safety_net("put on some jazz", None, None) == (
        "music_assistant",
        {"action": "play", "query": "jazz"},
    )
    assert b._music_safety_net("can you play despacito", None, None) == (
        "music_assistant",
        {"action": "play", "query": "despacito"},
    )
    assert b._music_safety_net("queue up phonk", None, None) == (
        "music_assistant",
        {"action": "play", "query": "phonk"},
    )


def test_music_safety_net_no_false_positives() -> None:
    """Non-play utterances are left untouched — the net must not hijack
    deal queries, figures of speech, or casual chat into a play."""
    b = _brain()
    for msg in ("flip a coin", "what is on my wishlist", "how are you"):
        assert b._music_safety_net(msg, None, None) == (None, None)


def test_music_safety_net_never_overrides_an_existing_tool() -> None:
    """If the LLM already chose a tool, the net is a no-op (it only fills
    the gap when the model returned no tool)."""
    b = _brain()
    assert b._music_safety_net("play x", "deal_assistant", {"request": "x"}) == (
        "deal_assistant",
        {"request": "x"},
    )


# --- _music_safety_net: bare-stop override (2026-07-06 prod regression) -----
# gemini-2.5-flash-lite routed a bare "stop." to {action: autoplay, mode: on,
# name: stop} -- there's no explicit "stop" example in the routing prompt to
# anchor on. This is the ONE exception to "never overrides an existing tool":
# a bare, unambiguous stop phrase overrides regardless, since there's no other
# plausible reading. See docs/incidents/bare-stop-command-misrouted-to-autoplay.md.


def test_music_safety_net_overrides_misrouted_stop_command() -> None:
    """The exact prod regression: a bare 'stop.' routed to the wrong action
    entirely must be corrected to action=stop, overriding whatever the LLM
    (or a weak fallback rung) actually returned."""
    b = _brain()
    assert b._music_safety_net(
        "stop.", "music_assistant", {"action": "autoplay", "mode": "on"}
    ) == (
        "music_assistant",
        {"action": "stop"},
    )


def test_music_safety_net_overrides_stop_when_no_tool_at_all() -> None:
    b = _brain()
    assert b._music_safety_net("stop", None, None) == ("music_assistant", {"action": "stop"})


@pytest.mark.parametrize(
    "phrase",
    [
        "stop",
        "stop.",
        "Stop!",
        "  STOP  ",
        "stop it",
        "stop the music",
        "stop the song",
        "stop playing",
    ],
)
def test_music_safety_net_recognizes_bare_stop_variants(phrase: str) -> None:
    b = _brain()
    assert b._music_safety_net(phrase, "music_assistant", {"action": "autoplay"}) == (
        "music_assistant",
        {"action": "stop"},
    )


def test_music_safety_net_leaves_correct_stop_routing_alone() -> None:
    """No spurious log/behavior difference when the LLM already got it right."""
    b = _brain()
    assert b._music_safety_net("stop", "music_assistant", {"action": "stop"}) == (
        "music_assistant",
        {"action": "stop"},
    )


def test_music_safety_net_does_not_override_non_bare_stop_phrasing() -> None:
    """Only the EXACT bare phrase overrides — 'stop the effects' or a longer
    sentence containing 'stop' keeps the LLM's routing, since those have other
    plausible readings (e.g. apply_effect) the narrow bare-phrase list must
    not swallow."""
    b = _brain()
    assert b._music_safety_net(
        "stop the effects please",
        "music_assistant",
        {"action": "apply_effect", "effect": "none"},
    ) == ("music_assistant", {"action": "apply_effect", "effect": "none"})


# --- _scrub_music_query: strip the user's intent verb, not the song ---------


def test_scrub_music_query_strips_leading_verb_only() -> None:
    """The leading intent verb (+ its prep) is stripped; the rest of the
    query — including a stray 'some' the verb regex does not cover — is
    preserved. Pins ACTUAL behavior (the docstring's 'jazz' example was
    wrong; it returns 'some jazz')."""
    scrub = PoobBrain._scrub_music_query
    assert scrub("play red hot chili peppers") == "red hot chili peppers"
    assert scrub("queue despacito") == "despacito"
    assert scrub("queue up some jazz") == "some jazz"  # NOT 'jazz' — verb+prep only
    assert scrub("Can't Stop") == "Can't Stop"  # no leading verb → unchanged
    assert scrub("") == ""


# --- empty/short play-query gate (STT cutoff: "Hey Poob. Play") -------------


@pytest.mark.asyncio
async def test_empty_short_play_query_prompts_instead_of_fanning_out() -> None:
    """A play with an empty/one-char query (STT cut off mid-sentence) must
    ask 'Play what?' (text) / stay silent (voice) and NEVER reach the music
    handler / ytdl."""
    b = _brain()
    handler = MagicMock()
    b._music_handler = handler

    text = await b._handle_music(
        "Hey Poob play",
        "u",
        voice=False,
        max_tok=80,
        tool_args={"action": "play", "query": ""},
    )
    assert text == "Play what?"

    voice = await b._handle_music(
        "Hey Poob play",
        "u",
        voice=True,
        max_tok=80,
        tool_args={"action": "play", "query": "a"},
    )
    assert voice == ""
    assert handler.mock_calls == []  # never fanned out to the music handler


# --- _TOOL_DETECTION_MAX_TOKENS floor (mid-arguments truncation guard) ------


@pytest.mark.asyncio
async def test_tool_detection_token_floor_overrides_small_response_cap() -> None:
    """Tool-call emission gets >=256 tokens even when the casual response cap
    is tiny (80) — conflating them truncated gpt-oss-20b mid-arguments and
    triggered Groq tool_use_failed 400s (poob.py:2070)."""
    b = _brain(groq_api_key="gk")
    seen: list[int] = []

    async def fake_call(provider, model, messages, tools, max_tokens):  # type: ignore[no-untyped-def]
        seen.append(max_tokens)
        return "", "music_assistant", {"action": "skip"}

    b._call_provider_with_tools = fake_call  # type: ignore[method-assign]
    await b._groq_with_tools([{"role": "user", "content": "skip"}], max_tokens=80)

    assert seen[0] == 256


# --- looks_tool_worthy gate: the DEAL side (music side was already tested) --


@pytest.mark.asyncio
async def test_deal_tool_worthy_query_continues_past_no_tool_rung() -> None:
    """A clear deal query ("what's on my wishlist") that a weak rung answers
    with no tool must NOT end the cascade — the deal tool_signals make it
    tool-worthy so the next rung gets a shot. Mirrors the music-side
    test_no_tool_does_not_end_cascade_for_control_while_music_plays."""
    b = _brain(groq_api_key="gk", google_api_key="g-key", nvidia_api_key="nk")
    calls: list[str] = []

    async def fake_call(provider, model, messages, tools, max_tokens):  # type: ignore[no-untyped-def]
        calls.append(model)
        if len(calls) == 1:
            return "let me think", None, None  # weak rung: no tool
        return "", "deal_assistant", {"request": "what is on my wishlist"}

    b._call_provider_with_tools = fake_call  # type: ignore[method-assign]
    messages = [
        {"role": "system", "content": "persona, nothing playing"},
        {"role": "user", "content": "what is on my wishlist"},
    ]
    _text, name, args = await b._groq_with_tools(messages, max_tokens=80)

    assert name == "deal_assistant"
    assert len(calls) >= 2


# --- _build_messages: routing-prompt context injection ----------------------


def _system_of(messages: list[dict]) -> str:
    return next(m["content"] for m in messages if m["role"] == "system")


def test_active_deal_session_hint_injected_into_routing_prompt() -> None:
    """An open deal session for (guild, user) appends the ACTIVE DEAL SESSION
    follow-up hint so a bare reply ("the blue one") still routes to the deal
    agent. This hint is routing-only (dropped on the casual path)."""
    b = _brain()
    b._deal_context[(0, "u")] = "Did you mean the Xbox Series X or S?"
    messages = b._build_messages("u", "the blue one", voice=False, guild_id=0)
    sys = _system_of(messages)
    assert "[ACTIVE DEAL SESSION" in sys
    assert "call deal_assistant with their full response" in sys


def test_music_context_block_marker_and_clauses_preserved() -> None:
    """The music-context block carries the literal 'MUSIC IS CURRENTLY
    PLAYING' marker (the exact substring the control_signals gate at
    poob.py:2049 keys off) plus the CONTROL (must-call) and INFO (no-tool)
    clauses. This string is a hard contract — see open risk #2 in the plan."""
    b = _brain()
    b._set_music_playing_info(0, "Daft Punk - One More Time [3:58]")
    messages = b._build_messages("u", "skip", voice=True, guild_id=0)
    sys = _system_of(messages)
    assert "MUSIC IS CURRENTLY PLAYING" in sys
    assert "you MUST call music_assistant" in sys
    assert "Do NOT call a tool and do NOT search" in sys


# --- DEAL_TOOL schema is the deal-routing surface --------------------------


def test_deal_tool_advertises_all_trigger_tokens() -> None:
    """DEAL_TOOL.description IS the deal-routing surface (function-calling
    reads it natively). Guard that every trigger token stays present — a
    prompt-content regression guard mirroring the MUSIC_TOOL guards."""
    desc = DEAL_TOOL["function"]["description"].lower()
    for token in (
        "wishlist",
        "watchlist",
        "show my",
        "add",
        "clear list",
        "scan",
        "find deals",
        "price",
        "when in doubt",
        "verbatim",
    ):
        assert token in desc, f"DEAL_TOOL.description lost trigger token: {token!r}"
