"""Tests for the slim routing prompt (docs/plans/slim-routing-prompt.md).

The routing call and the casual reply are SEPARATE LLM calls: the routing
call's text is discarded (poob.py:1024/937) and casual is regenerated with
the FULL persona prompt via _rebuild_messages_no_tools -> _build_system_prompt
(with_tools=False). So persona / RULES / NEVER / VOICE text is dead weight on
the routing call. `_build_routing_prompt` carries ONLY the tool-routing rules
(byte-identical to the with_tools block) plus a short classifier framing.

Losslessness is guaranteed two ways here:
1. The music/deal routing rules in the slim prompt are the SAME constants used
   by _build_system_prompt(with_tools=True) — byte-identical routing instructions.
2. The generation-only text is still present on the casual + wrap paths
   (guarded below), so nothing user-facing is lost.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from poob.brain.poob import (
    _DEAL_ROUTING_RULE,
    _MUSIC_ROUTING_RULES,
    _build_routing_prompt,
    _build_system_prompt,
)
from poob.brain.poob import PoobBrain


def _brain(**kw: Any) -> PoobBrain:
    return PoobBrain(deal_agent=None, **kw)


# Every routing rule / example that must survive in the slim prompt.
_MUSIC_RULE_SUBSTRINGS = [
    "Only use when explicitly asked.",
    "don't ask clarifying questions, just call it",
    "Whatever follows play / put on / queue IS the song",
    "THIS message only",
    "never pull a song title from earlier turns",
    "AUDIO EFFECTS ROUTING",
    "STACKING:",
    "mode='replace'",
    "do NOT fire this just because the word",
    "VOLUME IS NOT AN EFFECT",
    "NEVER apply_effect",
    "Be DILIGENT about catching real song requests",
    "play tiki tiki",
    "play cheeky cheeky",
    "music the user wants to HEAR",
    "not everything with the word 'play' is music",
    "flip a coin",
    "let's play a game",
    "good play",
    "play it cool",
]

# Generation-only text that must NOT appear in the routing prompt.
_GENERATION_ONLY_SUBSTRINGS = [
    "loud, bold",
    "horniness level",
    "RULES:",
    "NEVER:",
    "as an AI",
    "spurradic",
    "avoid vocatives",
    "VOICE MODE",
    "Match the energy",
]


# --- the slim routing prompt itself -----------------------------------------

def test_routing_prompt_keeps_every_music_routing_rule() -> None:
    p = _build_routing_prompt(with_music=True)
    for s in _MUSIC_RULE_SUBSTRINGS:
        assert s in p, f"routing prompt dropped a routing rule: {s!r}"
    assert _DEAL_ROUTING_RULE in p


def test_routing_prompt_omits_generation_only_text() -> None:
    p = _build_routing_prompt(with_music=True)
    for s in _GENERATION_ONLY_SUBSTRINGS:
        assert s not in p, f"routing prompt still carries generation-only text: {s!r}"


def test_routing_prompt_drops_music_block_when_no_handler() -> None:
    """with_music=False (text/DM, no music handler — mirrors the conditional
    MUSIC_TOOL wiring at poob.py:1978-1980) emits only the framing + the deal
    line; the whole music block is omitted."""
    p = _build_routing_prompt(with_music=False)
    assert _DEAL_ROUTING_RULE in p
    assert "music_assistant" not in p
    assert "nightcore" not in p
    assert "tiki tiki" not in p


def test_routing_rules_are_byte_identical_in_full_and_slim_prompts() -> None:
    """The losslessness guarantee: the routing instructions in the slim prompt
    are the SAME constants the full (with_tools=True) prompt uses — so the
    model sees identical routing guidance either way."""
    slim = _build_routing_prompt(with_music=True)
    full = _build_system_prompt(5, voice=True, with_tools=True)
    assert _MUSIC_ROUTING_RULES in slim
    assert _MUSIC_ROUTING_RULES in full
    assert _DEAL_ROUTING_RULE in slim
    assert _DEAL_ROUTING_RULE in full


def test_routing_prompt_is_substantially_shorter() -> None:
    slim = _build_routing_prompt(with_music=True)
    full = _build_system_prompt(5, voice=True, with_tools=True)
    # The persona + RULES + NEVER + VOICE blocks are gone; routing keeps only
    # the tool rules + a short framing.
    assert len(slim) < 0.6 * len(full)


# --- guard the MOVE: generation-only text survives on the generation paths --

def test_casual_generation_prompt_still_carries_persona_and_rules() -> None:
    """_build_system_prompt(with_tools=False) is the casual reply prompt
    (via _rebuild_messages_no_tools / _casual_text_fallback). Every clause the
    audit moved off routing must still live here."""
    c = _build_system_prompt(5, voice=False, with_tools=False)
    for s in (
        "loud, bold", "RULES:", "NEVER:", "as an AI", "I cannot",
        "don't make stuff up", "spurradic", "avoid vocatives", "crude",
        "Reference things nobody actually said",
    ):
        assert s in c, f"casual prompt lost a moved clause: {s!r}"
    # Casual is tool-free — no tool routing text leaks into the reply model.
    assert "music_assistant" not in c
    assert "deal_assistant" not in c


def test_voice_generation_prompt_still_carries_voice_block() -> None:
    v = _build_system_prompt(5, voice=True, with_tools=False)
    for s in ("spoken out loud", "ALL CAPS", "markdown", "Match the energy"):
        assert s in v


def test_full_system_prompt_with_tools_output_unchanged_by_refactor() -> None:
    """Factoring the routing rules into constants must not change the
    with_tools=True output — existing prompt-content tests + the wrap
    generators (poob.py:1875/1922) depend on it byte-for-byte."""
    full = _build_system_prompt(5, voice=True, with_tools=True)
    # the exact deal->music join the old single-literal produced
    assert "shopping stuff. Only use when explicitly asked.\nYou have a music_assistant" in full
    # persona still present (wrap generators rely on with_tools=True default)
    assert "loud, bold" in full


# --- wiring: _build_messages routing path uses the slim prompt --------------

def test_build_messages_routing_prompt_is_slim_with_handler() -> None:
    b = _brain()
    b._music_handler = MagicMock()  # with_music=True
    msgs = b._build_messages("u", "skip", voice=True, guild_id=0)
    sys = next(m["content"] for m in msgs if m["role"] == "system")
    assert "music_assistant" in sys
    assert "VOLUME IS NOT AN EFFECT" in sys
    assert "loud, bold" not in sys      # persona is NOT in the routing prompt
    assert "NEVER:" not in sys


def test_build_messages_routing_prompt_drops_music_without_handler() -> None:
    b = _brain()                         # default: no music handler
    assert b._music_handler is None
    msgs = b._build_messages("u", "what's on my list", voice=False, guild_id=0)
    sys = next(m["content"] for m in msgs if m["role"] == "system")
    assert _DEAL_ROUTING_RULE in sys
    assert "music_assistant" not in sys
