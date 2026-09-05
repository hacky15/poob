"""Regression test: the router-truncation guard (query extension) was
defeating duplicate-play suppression.

Production, 2026-08-31, the FIRST request of a fresh voice session:

    01:29:22  "Hey, Poob. Play crank that by pickle."
              -> safety net routed query='crank that by pickle' (no extension
                 fired for this call -- the raw message carried no extra
                 trailing words beyond the routed query)
              -> Queued "Pickle - Crank That" (a literal-token-match junk hit)

    01:29:38  "Hey Poob play crank that by pickle Oh, yeah, dude."  (+16s,
              well inside the 20s dedup window; the SAME user reacting to
              hearing the wrong song and re-asking, nearly verbatim)
              -> LLM routed query='crank that by pickle'  <- IDENTICAL to the
                 first request's routed query
              -> _prefer_full_play_span extended it to 'crank that by pickle
                 Oh, yeah, dude' (a legitimate router-truncation-guard fire --
                 the raw message really does carry that trailing span)
              -> Queued "Soulja Boy Tell'em - Crank That" (the correct song,
                 but a SECOND song, on top of the first)

Root cause: `_is_duplicate_play` runs AFTER `query` is reassigned to the
extended value. Both call sites (_handle_music, _handle_music_voice_streaming)
do `query = extended` before the dedup check, so dedup compares the SECOND
request's EXTENDED query against the FIRST request's un-extended recorded
key -- 'crank that by pickle Oh, yeah, dude' != 'crank that by pickle' -- and
never catches what is, at the routing level, an exact repeat.

This is a different shape than the STT-garble case in
docs/research/voice-session-audit-2026-07-29.md ("Diddy Heilett" vs "Diddy
Heil Epstein" -- genuinely different strings even before any extension,
correctly left unfixed). Here the ROUTED query is byte-identical both times;
only a downstream, unrelated correction (extension) breaks the match.

Fix: dedup keys on the query as originally routed (post-scrub, pre-extension)
-- the same identity a stable retry would naturally reproduce -- while the
extended/corrected query is still used, unchanged, for the actual search.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from poob.brain.poob import PoobBrain


def _make_brain() -> tuple[PoobBrain, list[str]]:
    """A brain wired to a music handler that records every query it was
    actually invoked with -- the most direct, unambiguous signal of whether
    a second (duplicate) call reached the handler at all."""
    brain = PoobBrain(groq_api_key="test-key", deal_agent=None)
    calls: list[str] = []

    async def _music_handler(*args: Any, **kwargs: Any) -> str:
        # Real call convention (see _handle_music / _handle_music_voice_streaming):
        # self._music_handler(original_message, int(user_id), guild_id,
        #                      voice=voice, tool_args=tool_args)
        tool_args = kwargs.get("tool_args") or {}
        query = tool_args.get("query", "")
        calls.append(query)
        return f"Queued track for {query!r} [3:00]"

    brain.set_music_handler(_music_handler)
    return brain, calls


@pytest.mark.asyncio
async def test_extension_does_not_defeat_dedup_in_handle_music() -> None:
    """The exact prod sequence, text/non-streaming path. The music handler
    must be invoked exactly ONCE -- the second call's extension changing the
    query string must not let it slip past dedup as if it were a new song."""
    brain, calls = _make_brain()
    guild_id = 10
    user_id = "394979315332284458"

    await brain._handle_music(
        "Hey, Poob. Play crank that by pickle.",
        user_id,
        True,  # voice
        200,  # max_tok
        {"action": "play", "query": "crank that by pickle"},
        guild_id=guild_id,
    )
    await brain._handle_music(
        "Hey Poob play crank that by pickle Oh, yeah, dude.",
        user_id,
        True,  # voice
        200,  # max_tok
        {"action": "play", "query": "crank that by pickle"},
        guild_id=guild_id,
    )

    assert calls == ["crank that by pickle"], (
        f"music handler invoked {len(calls)}x with {calls!r} -- expected exactly "
        f"1 call; query extension defeated dedup, reproducing the 2026-08-31 "
        f"double-queue ('Pickle - Crank That' + 'Soulja Boy ... Crank That' both "
        f"queued from what was, at the routing level, the same repeated request)"
    )


@pytest.mark.asyncio
async def test_extension_does_not_defeat_dedup_in_voice_streaming() -> None:
    """Same sequence, voice-streaming path (_handle_music_voice_streaming) --
    the second call site with the identical extension-before-dedup ordering.
    The handler here is fanned out via asyncio.create_task (speculative wrap),
    so the generator must be fully drained AND the background task awaited
    before the call count is trustworthy."""
    brain, calls = _make_brain()
    guild_id = 702353477602377769
    user_id = "394979315332284458"

    async for _ in brain._handle_music_voice_streaming(
        "Hey, Poob. Play crank that by pickle.",
        user_id,
        200,
        {"action": "play", "query": "crank that by pickle"},
        guild_id=guild_id,
    ):
        pass
    await asyncio.sleep(0)  # let the fanned-out music_task actually run

    async for _ in brain._handle_music_voice_streaming(
        "Hey Poob play crank that by pickle Oh, yeah, dude.",
        user_id,
        200,
        {"action": "play", "query": "crank that by pickle"},
        guild_id=guild_id,
    ):
        pass
    await asyncio.sleep(0)

    assert calls == ["crank that by pickle"], (
        f"music handler invoked {len(calls)}x with {calls!r} -- expected exactly "
        f"1 call; query extension defeated dedup in the voice-streaming path"
    )


def test_dedup_key_is_stable_across_extension() -> None:
    """Narrower unit-level guard on the actual mechanism: recording a play
    with the PRE-extension query, then checking with a query that would only
    match post-extension, must still be caught as a duplicate. This is the
    precise invariant _is_duplicate_play's docstring promises ("matches a
    previous play") and extension must not be allowed to break."""
    brain = PoobBrain(groq_api_key="test-key", deal_agent=None)
    guild_id, user_id = 10, "u1"

    assert brain._is_duplicate_play(guild_id, user_id, "crank that by pickle") is False
    # A second call within the window, using the SAME pre-extension identity,
    # must be caught -- this is the key invariant regardless of what any
    # particular call site does with extension afterward.
    assert brain._is_duplicate_play(guild_id, user_id, "crank that by pickle") is True
