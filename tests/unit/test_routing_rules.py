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
from unittest.mock import AsyncMock, MagicMock, patch

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


# --- _music_safety_net: autoplay/loop overrides (2026-07 prod regression) ---
# The weak fallback rung confuses autoplay <-> loop (no anchoring example in
# the routing prompt): "turn autoplay on" routed to {loop, off}, which the
# handler then cycled into LOOP_ONE — the same song looping forever while new
# requests never played. These exact phrases have no other plausible reading,
# so the net forces the right control (mirrors the bare-stop override).
# See docs/incidents/autoplay-request-enables-loop-one.md.


def test_music_safety_net_forces_autoplay_on() -> None:
    b = _brain()
    for phrase in (
        "autoplay on",
        "turn on autoplay",
        "turn autoplay on",
        "enable autoplay",
        "start autoplay",
    ):
        assert b._music_safety_net(phrase, None, None) == (
            "music_assistant",
            {"action": "autoplay", "mode": "on"},
        ), phrase


def test_music_safety_net_forces_autoplay_on_over_misrouted_loop() -> None:
    """The exact prod class: 'turn autoplay on' was routed to {loop, off}.
    The override wins even over a present (wrong) tool call."""
    b = _brain()
    assert b._music_safety_net(
        "turn autoplay on", "music_assistant", {"action": "loop", "mode": "off"}
    ) == ("music_assistant", {"action": "autoplay", "mode": "on"})


def test_music_safety_net_forces_autoplay_off() -> None:
    b = _brain()
    for phrase in (
        "autoplay off",
        "turn off autoplay",
        "disable autoplay",
        "stop autoplaying",
    ):
        assert b._music_safety_net(phrase, None, None) == (
            "music_assistant",
            {"action": "autoplay", "mode": "off"},
        ), phrase


def test_music_safety_net_forces_loop_off() -> None:
    b = _brain()
    for phrase in ("turn off loop", "loop off", "stop looping", "no loop"):
        assert b._music_safety_net(phrase, None, None) == (
            "music_assistant",
            {"action": "loop", "mode": "off"},
        ), phrase


def test_music_safety_net_loop_off_overrides_misrouted_cycle() -> None:
    """'turn off loop' forces mode=off even if the LLM routed a bare
    {loop} (which the handler would otherwise cycle)."""
    b = _brain()
    assert b._music_safety_net("turn off loop", "music_assistant", {"action": "loop"}) == (
        "music_assistant",
        {"action": "loop", "mode": "off"},
    )


def test_music_safety_net_autoplay_loop_no_false_positives() -> None:
    """Non-control phrasing must NOT be hijacked into autoplay/loop control.
    'play autoplay ...' is a real play; 'loop me in' is not a music command."""
    b = _brain()
    for msg in (
        "play autoplay by some band",
        "loop me in on the plan",
        "keep the loop tight",
        "how are you",
    ):
        _tool, args = b._music_safety_net(msg, None, None)
        assert args != {"action": "autoplay", "mode": "on"}, msg
        assert args != {"action": "autoplay", "mode": "off"}, msg
        assert args != {"action": "loop", "mode": "off"}, msg


# --- _music_safety_net: volume/skip control overrides, wake-aware -----------
# 2026-07-11 prod: the_._gamer said "Hey, Poob. Max volume." and the weak
# fallback rung (gemini-2.5-flash-lite) routed {action: skip} — the song got
# skipped instead of louder. Two structural gaps made the old net useless
# here: it only ran when NO tool was routed, and it couldn't see past the
# "Hey, Poob." wake prefix the voice transcript keeps. The unified override
# now runs on every routing result and strips wake/attribution heads before
# the exact-phrase match. See
# docs/incidents/control-command-misroute-by-weak-rung.md.


def test_control_override_corrects_max_volume_misrouted_to_skip() -> None:
    """The exact prod failure, wake prefix and all."""
    b = _brain()
    assert b._music_safety_net("Hey, Poob. Max volume.", "music_assistant", {"action": "skip"}) == (
        "music_assistant",
        {"action": "volume", "value": 200},
    )


def test_control_override_volume_phrases() -> None:
    b = _brain()
    for phrase, expected in (
        ("max volume", {"action": "volume", "value": 200}),
        ("full volume", {"action": "volume", "value": 200}),
        ("mute", {"action": "volume", "value": 0}),
        ("louder", {"action": "volume_up"}),
        ("turn it up", {"action": "volume_up"}),
        ("quieter", {"action": "volume_down"}),
        ("turn it down", {"action": "volume_down"}),
    ):
        assert b._music_safety_net(phrase, None, None) == (
            "music_assistant",
            expected,
        ), phrase


def test_control_override_skip_phrases_wake_aware() -> None:
    b = _brain()
    for msg in ("skip", "Poob, skip.", "hey poob skip", "next song", "skip this one"):
        assert b._music_safety_net(msg, None, None) == (
            "music_assistant",
            {"action": "skip"},
        ), msg


def test_control_override_sees_past_speaker_attribution_on_voice() -> None:
    """Voice without a passive transcript prepends 'Name: ' — the override
    must still see the command underneath (voice=True)."""
    b = _brain()
    assert b._music_safety_net("Ben: Hey, Poob. Max volume.", None, None, voice=True) == (
        "music_assistant",
        {"action": "volume", "value": 200},
    )


def test_control_override_does_NOT_strip_attribution_on_text() -> None:
    """Regression (review-caught): the attribution stripper matched ANY short
    'word: ' head, so ordinary TEXT chat whose tail is a control phrase got
    force-executed. On the text path (voice=False) the 'note to self:'/'edit:'
    head is real content and must NOT be stripped — the LLM's routing stands.
    See docs/incidents/control-command-misroute-by-weak-rung.md."""
    b = _brain()
    for msg in (
        "note to self: skip this song",
        "edit: next",
        "fyi: mute",
        "my vote: louder",
        "ps: skip this",
    ):
        # No override on text — attribution head is not stripped.
        assert b._match_control_override(msg, voice=False) is None, msg
        # And with no tool routed, the net leaves it alone (falls to no-op).
        assert b._music_safety_net(msg, None, None, voice=False) == (None, None), msg
    # The SAME shape on the voice path (a real 'Speaker: ' prepend) DOES strip.
    assert b._match_control_override("Ben: skip this song", voice=True) == {"action": "skip"}


def test_control_override_never_clobbers_a_routed_deal() -> None:
    """Regression (review-caught): the unconditional net must NOT hijack a
    correctly-routed deal_assistant. During a deal Q&A the router sends a
    terse 'stop'/'skip'/'next' answer to the deal agent — overriding it into a
    music action silently drops the deal command.
    See docs/incidents/control-command-misroute-by-weak-rung.md."""
    b = _brain()
    for phrase in ("stop", "skip", "next", "mute"):
        assert b._music_safety_net(phrase, "deal_assistant", {"request": phrase}) == (
            "deal_assistant",
            {"request": phrase},
        ), phrase
    # But it STILL corrects a missing route and a music<->music misroute.
    assert b._music_safety_net("skip", None, None) == ("music_assistant", {"action": "skip"})
    assert b._music_safety_net("max volume", "music_assistant", {"action": "skip"}) == (
        "music_assistant",
        {"action": "volume", "value": 200},
    )


def test_control_override_stop_now_fires_on_voice_wake_prefix() -> None:
    """The bare-stop net was text-only before (exact match failed on the
    wake prefix); the unified matcher covers voice too."""
    b = _brain()
    assert b._music_safety_net("Hey, Poob. Stop.", "music_assistant", {"action": "autoplay"}) == (
        "music_assistant",
        {"action": "stop"},
    )


def test_control_override_leaves_longer_sentences_alone() -> None:
    """Only the WHOLE remaining command matches — sentences that merely
    contain a control word keep the LLM's routing. (Messages with play
    intent still hit the pre-existing play backfill when NO tool routed —
    that behavior is separate and unchanged.)"""
    b = _brain()
    for msg in (
        "what's next on the agenda",
        "the volume knob on my amp broke",
        "2:30",
    ):
        assert b._music_safety_net(msg, None, None) == (None, None), msg
    # With a tool already present, NONE of these longer sentences (play-
    # flavored or not) get overridden — the control override is exact-match
    # only and the play backfill never touches a present tool.
    for msg in (
        "what's next on the agenda",
        "play the next song by AJR",
        "skip the intro and play the chorus",
        "the volume knob on my amp broke",
        "2:30",
    ):
        assert b._music_safety_net(msg, "music_assistant", {"action": "play", "query": "x"}) == (
            "music_assistant",
            {"action": "play", "query": "x"},
        ), msg


def test_control_override_no_log_when_routing_was_already_right() -> None:
    """Same action already routed -> forced args still returned (normalizes
    value) but it's not a 'misroute' — behavior contract only, no crash."""
    b = _brain()
    tool, args = b._music_safety_net("skip", "music_assistant", {"action": "skip"})
    assert (tool, args) == ("music_assistant", {"action": "skip"})


def test_music_routing_rules_distinguish_autoplay_from_loop() -> None:
    """The routing prompt must carry explicit, separate anchors for autoplay
    and loop so a weak rung stops collapsing one into the other."""
    from poob.brain.poob import _MUSIC_ROUTING_RULES

    rules = _MUSIC_ROUTING_RULES.lower()
    assert "action=autoplay" in rules
    assert "action=loop" in rules
    assert "mode=one" in rules
    assert "mode=queue" in rules


def test_looks_like_playable_link_matches_ytdl_recognition() -> None:
    """The url->query promotion predicate must recognize exactly what the
    resolver can play — including scheme-less YouTube forms — so a link the
    router put in tool_args['url'] doesn't dead-end at 'Play what?'.
    Review-caught: it previously only accepted http/https/spotify schemes."""
    from poob.brain.poob import _looks_like_playable_link

    for link in (
        "https://open.spotify.com/track/abc?si=x",
        "spotify:track:abc",
        "http://youtu.be/dQw4w9WgXcQ",
        "https://www.youtube.com/watch?v=abc",
        "www.youtube.com/watch?v=abc",  # scheme-less
        "youtu.be/dQw4w9WgXcQ",  # scheme-less — the gap that dead-ended
        "youtube.com/watch?v=abc",  # scheme-less
    ):
        assert _looks_like_playable_link(link) is True, link
    for not_link in ("tiki tiki", "play some jazz", "", "backwoods 808 fishing"):
        assert _looks_like_playable_link(not_link) is False, not_link


# --- _is_content_free: bare-address guard ------------------------------------
# 2026-07-11 prod: user spoke for 9.7s but STT only produced "Hey, Poob." —
# the router back-filled {autoplay, on} from his request 17 minutes earlier
# and acknowledged it silently. A content-free address must never reach tool
# routing. See docs/incidents/bare-wake-address-routes-hallucinated-tool.md.


def test_is_content_free_on_bare_addresses() -> None:
    b = _brain()
    # Wake-only reductions are content-free regardless of path.
    for msg in ("Hey, Poob.", "Poob", "hey poob!!!", "Poob?"):
        assert b._is_content_free(msg, voice=False) is True, msg
        assert b._is_content_free(msg, voice=True) is True, msg
    # The 'Speaker: ' voice prepend only reduces to empty on the voice path.
    assert b._is_content_free("Ben: Hey, Poob.", voice=True) is True
    assert b._is_content_free("Ben: Hey, Poob.", voice=False) is False


def test_is_content_free_false_when_content_present() -> None:
    b = _brain()
    for msg in (
        "Hey, Poob. Max volume.",
        "Hey, Poob. Hmm.",
        "hi",
        "play tiki tiki",
        "Hey Poob play thirsty thirsty Thursday",
    ):
        assert b._is_content_free(msg, voice=True) is False, msg
        assert b._is_content_free(msg, voice=False) is False, msg


@pytest.mark.asyncio
async def test_content_free_address_skips_tool_routing_entirely() -> None:
    """respond() must not consult the tool router for a bare address — even
    a router that WOULD return a tool (the prod hallucination) is bypassed,
    and the casual reply path answers instead."""
    b = PoobBrain(deal_agent=None, groq_api_key="test-key")
    b._music_handler = object()  # music wired, so the temptation exists

    with (
        patch.object(
            b,
            "_groq_with_tools",
            new=AsyncMock(
                return_value=("", "music_assistant", {"action": "autoplay", "mode": "on"})
            ),
        ) as router,
        patch.object(b, "_casual_text_fallback", new=AsyncMock(return_value="what's up")),
        patch.object(b, "_handle_music", new=AsyncMock()) as handle_music,
    ):
        out = await b.respond("Hey, Poob.", user_id="u1", guild_id=10)

    router.assert_not_awaited()
    handle_music.assert_not_called()
    assert out == "what's up"


@pytest.mark.asyncio
async def test_message_with_content_still_routes_normally() -> None:
    """The guard is narrow: any real content keeps the normal routing path."""
    b = PoobBrain(deal_agent=None, groq_api_key="test-key")
    b._music_handler = object()

    with (
        patch.object(
            b,
            "_groq_with_tools",
            new=AsyncMock(
                return_value=("", "music_assistant", {"action": "play", "query": "tiki tiki"})
            ),
        ) as router,
        patch.object(
            b, "_handle_music", new=AsyncMock(return_value="Playing tiki tiki.")
        ) as handle_music,
    ):
        out = await b.respond("Hey, Poob. Play tiki tiki.", user_id="u1", guild_id=10)

    router.assert_awaited_once()
    handle_music.assert_called_once()
    assert out == "Playing tiki tiki."


# --- _music_safety_net: bare-autoplay override (2026-07-09 prod regression) -
# gemini-2.5-flash-lite routed "autoplay turn ON" to {action: apply_effect,
# effect: faster, mode: more} after several consecutive apply_effect calls
# biased the passive context. Same failure class and fix pattern as the
# bare-stop override above. See
# docs/incidents/autoplay-misrouted-to-apply-effect.md.


def test_music_safety_net_overrides_misrouted_autoplay_command() -> None:
    """The exact prod regression: 'autoplay turn ON' routed to apply_effect
    must be corrected to action=autoplay, mode=on."""
    b = _brain()
    assert b._music_safety_net(
        "autoplay turn ON",
        "music_assistant",
        {"action": "apply_effect", "effect": "faster", "mode": "more"},
    ) == ("music_assistant", {"action": "autoplay", "mode": "on"})


def test_music_safety_net_overrides_autoplay_when_no_tool_at_all() -> None:
    b = _brain()
    assert b._music_safety_net("autoplay on", None, None) == (
        "music_assistant",
        {"action": "autoplay", "mode": "on"},
    )


@pytest.mark.parametrize(
    ("phrase", "expected_mode"),
    [
        ("autoplay on", "on"),
        ("Autoplay Turn On", "on"),
        ("  TURN ON AUTOPLAY  ", "on"),
        ("turn autoplay on", "on"),
        ("enable autoplay", "on"),
        ("autoplay enable", "on"),
        ("autoplay off", "off"),
        ("autoplay turn off", "off"),
        ("turn off autoplay", "off"),
        ("turn autoplay off", "off"),
        ("disable autoplay", "off"),
        ("autoplay disable", "off"),
        ("autoplay status", "status"),
        ("is autoplay on", "status"),
        ("is autoplay on?", "status"),
    ],
)
def test_music_safety_net_recognizes_bare_autoplay_variants(
    phrase: str,
    expected_mode: str,
) -> None:
    b = _brain()
    assert b._music_safety_net(
        phrase,
        "music_assistant",
        {"action": "apply_effect", "effect": "overload"},
    ) == ("music_assistant", {"action": "autoplay", "mode": expected_mode})


def test_music_safety_net_leaves_correct_autoplay_routing_alone() -> None:
    b = _brain()
    assert b._music_safety_net(
        "autoplay on",
        "music_assistant",
        {"action": "autoplay", "mode": "on"},
    ) == ("music_assistant", {"action": "autoplay", "mode": "on"})


def test_music_safety_net_does_not_override_non_bare_autoplay_phrasing() -> None:
    """Only the EXACT bare phrase overrides — a longer sentence mentioning
    autoplay keeps the LLM's routing, since it may carry other real intent
    the narrow phrase list must not swallow."""
    b = _brain()
    assert b._music_safety_net(
        "turn off the autoplay filter thing please",
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


# --- _trim_trailing_crosstalk: safety-net play-query hygiene ------------------
# 2026-07-17 census: the take-everything-after-the-verb extraction shipped
# trailing crosstalk to YouTube search ("the song. Fuck.", "thirsty thirsty
# Thursday. Oh yeah. I I used to get those all the time...").


def test_trim_crosstalk_cuts_at_first_sentence_boundary() -> None:
    from poob.brain.poob import _trim_trailing_crosstalk

    assert _trim_trailing_crosstalk("the song. Fuck.") == "the song"
    assert (
        _trim_trailing_crosstalk("thirsty thirsty Thursday. Oh yeah. I I used to get those")
        == "thirsty thirsty Thursday"
    )


def test_trim_crosstalk_preserves_title_abbreviations() -> None:
    from poob.brain.poob import _trim_trailing_crosstalk

    assert _trim_trailing_crosstalk("mr. brightside") == "mr. brightside"
    # Abbreviation inside the title, real crosstalk after it — cut at the
    # boundary AFTER the title, not inside it.
    assert _trim_trailing_crosstalk("mr. brightside. yeah man") == "mr. brightside"


def test_trim_crosstalk_strips_trailing_punctuation_only_when_clean() -> None:
    from poob.brain.poob import _trim_trailing_crosstalk

    assert _trim_trailing_crosstalk("home or let the barts out.") == "home or let the barts out"
    assert _trim_trailing_crosstalk("Bitty Funk.") == "Bitty Funk"
    assert _trim_trailing_crosstalk("APT.") == "APT"
    assert _trim_trailing_crosstalk("gobble glitch") == "gobble glitch"


def test_safety_net_blank_content_span_defers_to_play_what() -> None:
    """'Play the song. Fuck.' trims to 'the song' — pure filler with no
    song identity. Better contract (2026-07-17 census): blank the query so
    the downstream empty-query gate asks 'Play what?' instead of literal-
    searching filler (which queued a novelty track titled 'FUCK!! Song!')."""
    b = _brain()
    tool, args = b._music_safety_net("Hey, Poob. Play the song. Fuck.", None, None, voice=True)
    assert tool == "music_assistant"
    assert args["query"] == ""


def test_safety_net_real_span_still_extracted_with_trim() -> None:
    b = _brain()
    tool, args = b._music_safety_net(
        "Hey, Poob. Play thirsty thirsty Thursday. Oh yeah. I used to get those",
        None,
        None,
        voice=True,
    )
    assert tool == "music_assistant"
    assert args["query"] == "thirsty thirsty Thursday"


# --- misrouted-play override (2026-07-16 02:15:49 stale-echo regression) ----
# gemini-2.5-flash-lite re-emitted its previous turn's apply_effect/slowed
# args for "Play Betty Davis eyes. Jojo Siwa." — the wrong action ran
# SILENTLY ([SILENT] control acks are unspoken) and the song never played.


def test_misrouted_play_override_rescues_explicit_play() -> None:
    b = _brain()
    tool, args = b._music_safety_net(
        "Play Betty Davis eyes. Jojo Siwa.",
        "music_assistant",
        {"action": "apply_effect", "effect": "slowed", "mode": "add"},
    )
    assert tool == "music_assistant"
    assert args["action"] == "play"
    assert args["query"] == "Betty Davis eyes"


def test_misrouted_play_override_never_clobbers_control_phrased_plays() -> None:
    """'play it slower' / 'play that again' are effect/restore requests
    phrased with 'play' — their correct routes must survive."""
    b = _brain()
    for msg, routed in (
        ("play it slower", {"action": "apply_effect", "effect": "slowed", "mode": "add"}),
        ("play it louder", {"action": "volume_up"}),
        ("play that again", {"action": "restore"}),
        ("play the next song", {"action": "skip"}),
    ):
        assert b._music_safety_net(msg, "music_assistant", dict(routed)) == (
            "music_assistant",
            routed,
        ), msg


def test_misrouted_play_override_leaves_play_and_deal_routes_alone() -> None:
    b = _brain()
    # Already a play — untouched.
    assert b._music_safety_net(
        "play despacito", "music_assistant", {"action": "play", "query": "despacito"}
    ) == ("music_assistant", {"action": "play", "query": "despacito"})
    # queue_many is play-family — untouched.
    assert b._music_safety_net(
        "play A and B", "music_assistant", {"action": "queue_many", "tracks": ["A", "B"]}
    ) == ("music_assistant", {"action": "queue_many", "tracks": ["A", "B"]})
    # Deal routes are never touched by any music override.
    assert b._music_safety_net("play despacito", "deal_assistant", {"request": "x"}) == (
        "deal_assistant",
        {"request": "x"},
    )


# --- silent-control veto (2026-09-09 prod: content present, but no evidence)-
# Two real production failures, same night: an addressed utterance with real
# words after "Hey, Poob" (not the bare-wake-address case _is_content_free
# already covers) got routed to a stateful control action with NOTHING in
# the message supporting it -- almost certainly a stale echo of an earlier
# turn's real command. Because these actions are [SILENT] (no spoken ack),
# the user heard nothing at all, both times:
#   "It doesn't. What'd you say, Logan? Hey, Poob." -> apply_effect/slowed
#   "K. Hey, Poob. You alive there?"                 -> apply_effect/slowed


def test_silent_control_veto_rejects_prod_regression_you_alive_there() -> None:
    """The exact second prod occurrence: no keyword anywhere in the message
    supports 'apply_effect' -> vetoed to (None, None), falls through to a
    normal casual reply instead of executing silently."""
    b = _brain()
    assert b._music_safety_net(
        "K. Hey, Poob. You alive there?",
        "music_assistant",
        {"action": "apply_effect", "effect": "slowed", "mode": "add"},
        voice=True,
    ) == (None, None)


def test_silent_control_veto_rejects_prod_regression_logan() -> None:
    """The exact first prod occurrence."""
    b = _brain()
    assert b._music_safety_net(
        "It doesn't. What'd you say, Logan? Hey, Poob.",
        "music_assistant",
        {"action": "apply_effect", "effect": "slowed", "mode": "add"},
        voice=True,
    ) == (None, None)


@pytest.mark.parametrize(
    "action_args",
    [
        {"action": "skip"},
        {"action": "pause"},
        {"action": "resume"},
        {"action": "stop"},
        {"action": "volume", "value": 100},
        {"action": "volume_up"},
        {"action": "volume_down"},
        {"action": "shuffle"},
        {"action": "loop", "mode": "one"},
        {"action": "apply_effect", "effect": "bassboost", "mode": "add"},
        {"action": "autoplay", "mode": "on"},
        {"action": "restore"},
        {"action": "replay"},
        {"action": "previous"},
        {"action": "seek", "time": "1:00"},
        {"action": "leave"},
    ],
)
def test_silent_control_veto_covers_every_silent_action(action_args: dict) -> None:
    """Every stateful, no-free-text-argument action is covered by the veto
    when the message carries zero supporting keywords."""
    b = _brain()
    assert b._music_safety_net(
        "so anyway did you see that meme earlier",
        "music_assistant",
        dict(action_args),
        voice=True,
    ) == (None, None), action_args


def test_silent_control_veto_does_not_fire_with_keyword_evidence() -> None:
    """A control action WITH real supporting language in the message is left
    alone -- the veto only fires on zero evidence, never on a legitimate but
    non-exact-phrase command (that's the router's job, not this guard's)."""
    b = _brain()
    assert b._music_safety_net(
        "hey poob can you add a bit more reverb to this",
        "music_assistant",
        {"action": "apply_effect", "effect": "reverb", "mode": "add"},
        voice=True,
    ) == ("music_assistant", {"action": "apply_effect", "effect": "reverb", "mode": "add"})


def test_silent_control_veto_never_touches_play_or_readonly_actions() -> None:
    """'play'/'queue_many' carry their own query as evidence and are excluded
    from this veto; read-only info actions are harmless even if imprecise."""
    b = _brain()
    for args in (
        {"action": "play", "query": "some obscure b-side"},
        {"action": "queue_many", "tracks": ["A", "B"]},
        {"action": "now_playing"},
        {"action": "list_effects"},
        {"action": "list_playlists"},
        {"action": "lyrics"},
        {"action": "queue"},
    ):
        assert b._music_safety_net(
            "so anyway did you see that meme earlier",
            "music_assistant",
            dict(args),
            voice=True,
        ) == ("music_assistant", args), args


def test_silent_control_veto_never_touches_deal_routes() -> None:
    """Scoped to music_assistant only -- never touches a routed deal query
    even if it happens to share an action-shaped dict."""
    b = _brain()
    assert b._music_safety_net(
        "so anyway did you see that meme earlier",
        "deal_assistant",
        {"request": "skip"},
        voice=True,
    ) == ("deal_assistant", {"request": "skip"})


# --- _looks_like_non_music_play_usage: opinion-question / game-reference /---
# --- figure-of-speech guard (2026-07-21 prod regression) --------------------
# "Hey, Poob. Do you think we should play hard or standard?" (asking Poob's
# OPINION on a BTD6 game-difficulty choice) — the ENTIRE cascade correctly
# returned no tool (no poob.tool_route line at all), then the play-intent
# backfill REVERSED that correct decision purely because " play " appeared
# in the sentence, and queued a random "Dark Path HARD STANDARD Guide" BTD6
# video. Neither play-related override may fire when 'play' isn't actually a
# music command in THIS message. See
# docs/incidents/play-question-misrouted-to-play-command.md and
# docs/decisions/music-routing-prompt-thoughtfulness.md (which flagged this
# exact risk class — "let's play a game" — when the backstop was kept).
#
# v2 (same-day rewrite): an adversarial review of the first version proved
# whole-phrase verb enumeration ("let's play"/"wanna play"/"should we play")
# both over-matches real requests ("I want to play some jazz") AND
# under-matches paraphrases of the actual incident ("what do you reckon,
# play X", one interposed word defeating "should we play"), and that
# unanchored whole-message matching let a trailing unrelated clause suppress
# a genuine LEADING play command.
#
# v3 (second adversarial review, before ship): v2's fixes were themselves
# unscoped in new ways — a whole-message idiom search, an unbounded lead-in,
# and priority- rather than position-ordered verb matching all reproduced
# the same "unrelated clause suppresses a real command" bug class in a new
# shape. See the incident note's "v3" section and the design comment above
# _NOT_MUSIC_LEADIN_RE in poob.py for the full rationale of what changed.


def test_opinion_question_play_usage_is_not_music() -> None:
    """Only multi-word opinion-eliciting phrases with near-zero collision
    risk against real commands are recognized — 'do you think'/'what do you
    reckon'/etc, checked against the LEAD-IN before the play verb, scoped to
    the CLAUSE containing the verb (v3: see
    test_comma_separated_opinion_anchor_is_a_documented_accepted_gap for why
    these are all punctuation-free between the anchor and the verb)."""
    from poob.brain.poob import _looks_like_non_music_play_usage

    for msg in (
        "Hey, Poob. Do you think we should play hard or standard?",
        "do you think we should play easy mode",
        "What do you think should we play defense?",
        "What do you reckon should we play attack or defense?",
        "What's your take should we play hard or standard?",
        "your thoughts on should we play offense",
    ):
        assert _looks_like_non_music_play_usage(msg) is True, msg


def test_comma_separated_opinion_anchor_is_a_documented_accepted_gap() -> None:
    """Deliberate v3 trade-off: the opinion lead-in check is scoped to the
    CLAUSE containing the play verb (_clause_bounds) so an unrelated clause
    can never suppress a real trailing command — the v3 fix for the second
    review's highest-severity false-positive ("What's your take on the new
    Kanye album, play Flashing Lights" must not be suppressed). The cost is
    that a genuinely comma-separated opinion question — anchor phrase and
    play verb in DIFFERENT clauses — is no longer caught by this guard. The
    real production incident had NO comma at all ("Do you think we should
    play hard or standard?"), so the primary case stays fixed; this variant
    falls through to the LLM router, which the incident's own log evidence
    shows handles thoughtful questions correctly already. Pinned here so a
    future change to this trade-off is deliberate, not accidental."""
    from poob.brain.poob import _looks_like_non_music_play_usage

    assert _looks_like_non_music_play_usage("What do you reckon, play attack or defense?") is False
    assert _looks_like_non_music_play_usage("your thoughts on, should we play offense") is False


def test_idiom_play_usage_is_not_music_regardless_of_framing() -> None:
    """Closed-set idioms ('play it cool'/'play it safe'/praise/'playing
    with') are recognized by their own content, independent of whatever
    opinion-question framing (or lack of it) wraps them — 'play it safe'
    catches 'I think we should play it safe' even though bare 'I think'
    isn't itself a recognized opinion lead-in."""
    from poob.brain.poob import _looks_like_non_music_play_usage

    for msg in (
        "play it cool",
        "play it safe",
        "play it real cool",
        "play it pretty safe",
        "I think we should play it safe",
        "you think we should play it safe",
        "good play",
        "nice play man",
        "great play right there",
        "solid play",
        "stop playing with me",
        "stop playing",
    ):
        assert _looks_like_non_music_play_usage(msg) is True, msg


def test_game_reference_span_is_not_music() -> None:
    """A play span whose only real content is a generic game-reference noun
    ('game'/'games') is not music — checked on the SPAN content, not the
    verb phrase, so it works regardless of which verb ('play'/'let's play'/
    'wanna play') introduces it, and tolerates a common adjective/quantifier
    modifier ('a FUN game', 'one MORE game') that a second adversarial
    review found defeating the exact-subset check."""
    from poob.brain.poob import _looks_like_non_music_play_usage

    for msg in (
        "let's play a game",
        "lets play a game",
        "play a game",
        "wanna play a game",
        "want to play some games",
        "let's play a fun game",
        "wanna play one more game",
        "let's play a quick game",
        "should we play another game",
    ):
        assert _looks_like_non_music_play_usage(msg) is True, msg


def test_round_and_match_game_words_are_a_documented_accepted_gap() -> None:
    """Deliberate v3 narrowing: 'round'/'match' were dropped from
    _GAME_REFERENCE_WORDS because a second adversarial review found them
    colliding with real song-title content ('Round and Round' — Ratt 1984 /
    Selena Gomez ft. Flo Rida). 'should we play a round' is no longer
    recognized as a game reference by this guard; deferred to the LLM/
    prompt layer, same reasoning as specific game titles. Pinned here so a
    future change to this trade-off is deliberate, not accidental."""
    from poob.brain.poob import _looks_like_non_music_play_usage

    assert _looks_like_non_music_play_usage("should we play a round") is False


def test_game_reference_word_no_longer_misclassifies_a_real_song() -> None:
    """The v3 fix for the second review's game-word/song-title collision
    finding: 'Round and Round' is a real song whose only surviving content
    token, after stopword stripping, used to be the bare word 'round' —
    which the old (v2) _GAME_REFERENCE_WORDS set then misclassified as
    'let's play a round [of a game]'."""
    from poob.brain.poob import _looks_like_non_music_play_usage

    assert _looks_like_non_music_play_usage("play round and round") is False


def test_specific_game_titles_are_a_documented_accepted_gap() -> None:
    """Deliberate trade-off (see the poob.py design note above
    _NOT_MUSIC_LEADIN_RE): the code-level backstop does NOT enumerate
    specific game titles ('Among Us') — a real 'Fortnite'/'Minecraft' SONG
    request would collide with that enumeration. This is left to the LLM/
    prompt layer, which the incident's own log evidence shows handles it
    correctly. Pinned here so a future change to this trade-off is
    deliberate, not accidental."""
    from poob.brain.poob import _looks_like_non_music_play_usage

    assert _looks_like_non_music_play_usage("wanna play Among Us") is False
    assert _looks_like_non_music_play_usage("want to play some Among Us") is False


# --- 2026-09-11 production evidence: bare WH-question + empty content -----
# "Hey, Poob. What Rainbow Six Siege map should we play on?" -- a real
# strategy question, no recognized opinion anchor -- fell through to the
# play backfill, extracted "on" as a song, found no content, and asked
# "play what?" out loud. This is exactly the residual gap
# test_documented_v1_bug_reports_are_fixed's sibling tests already flagged
# as "deferred to new production evidence" -- this is that evidence.


def test_wh_question_with_no_play_content_is_not_music() -> None:
    """The exact prod transcript, plus paraphrases: a WH-question (what/
    which) introducing the clause, with nothing but function words after
    the play verb, is recognized as non-music regardless of whether it
    matches one of the enumerated opinion-anchor phrases."""
    from poob.brain.poob import _looks_like_non_music_play_usage

    for msg in (
        "Hey, Poob. What Rainbow Six Siege map should we play on?",
        "What map should we play on?",
        "Which map should we play on",
        "What difficulty should we play on?",
        "Which one should we play with?",
    ):
        assert _looks_like_non_music_play_usage(msg) is True, msg


def test_wh_question_guard_does_not_touch_real_content() -> None:
    """The new guard only fires when NOTHING survives after the play verb --
    a genuine 'what should we play, X' request with real content (a song,
    artist, or genre) must still play, exactly like the pre-existing
    'should we play some Metallica' case."""
    from poob.brain.poob import _looks_like_non_music_play_usage

    for msg in (
        "What should we play, some jazz or rock?",
        "Which song should we play, Bohemian Rhapsody?",
        "what do you want to play, despacito or tiki tiki",
    ):
        assert _looks_like_non_music_play_usage(msg) is False, msg


def test_wh_question_guard_scoped_to_the_verbs_own_clause() -> None:
    """Same clause-scoping discipline as every other check here: an
    unrelated WH-question in an earlier clause must not suppress a real
    trailing play command in a different clause."""
    from poob.brain.poob import _looks_like_non_music_play_usage

    assert _looks_like_non_music_play_usage("What map are we on, play Bohemian Rhapsody") is False


def test_real_play_commands_are_not_flagged() -> None:
    """The guard must never suppress genuine play requests — including the
    documented vault catches, mid-sentence commands, and the exact phrasings
    the v1 regex incorrectly flagged (verb-only 'want to play'/'let's play'/
    'should we play' followed by REAL music content, not a game/idiom)."""
    from poob.brain.poob import _looks_like_non_music_play_usage

    for msg in (
        "play some jazz",
        "can you play despacito",
        "queue up phonk",
        "play tiki tiki",
        "hey poob play cheeky cheeky",
        "put on some music",
        "play body dee dum",
        "yeah that's cool, play some tiki tiki",
        "play starships",
        # v1 false positives (verb-phrase-only matching wrongly caught these)
        "I want to play some jazz",
        "let's play some tunes",
        "wanna play some Beyonce",
        "want to play my playlist",
        "should I play the new Drake album",
        "should we play some Metallica right now",
    ):
        assert _looks_like_non_music_play_usage(msg) is False, msg


def test_leading_play_command_survives_a_trailing_unrelated_clause() -> None:
    """Architectural regression guard (v1's most serious finding): the guard
    must be scoped to the play verb it's evaluating, not the whole message —
    a trailing 'wanna play something after' clause must NOT suppress a
    genuine LEADING play command earlier in the same (compound,
    STT-crosstalk-laden) message."""
    from poob.brain.poob import _looks_like_non_music_play_usage

    assert (
        _looks_like_non_music_play_usage(
            "Play Betty Davis eyes. Jojo Siwa. Wanna play something after"
        )
        is False
    )


def test_unrelated_leading_clause_does_not_suppress_a_real_trailing_command() -> None:
    """v3 fix (second review's highest-severity finding): the mirror image
    of the trailing-clause regression above, now on the LEADING side. The
    opinion lead-in check used to scan the ENTIRE prefix before the play
    verb, so an unrelated leading clause containing one of the anchor
    phrases ('your take on...') could suppress a real, unrelated trailing
    play command."""
    from poob.brain.poob import _looks_like_non_music_play_usage

    assert (
        _looks_like_non_music_play_usage(
            "What's your take on the new Kanye album, play Flashing Lights"
        )
        is False
    )
    assert (
        _looks_like_non_music_play_usage(
            "Your thoughts on the new Kanye album, play Flashing Lights"
        )
        is False
    )


def test_idiom_in_an_unrelated_clause_does_not_suppress_a_real_trailing_command() -> None:
    """v3 fix: _NOT_MUSIC_IDIOM_RE used to be checked unconditionally
    against the WHOLE message, so a 'stop playing X' clause could suppress a
    genuinely unrelated, real trailing play request in the same compound
    message — an ordinary control+request pattern for a voice music
    assistant, not a contrived edge case."""
    from poob.brain.poob import _looks_like_non_music_play_usage

    assert _looks_like_non_music_play_usage("Stop playing this, play some jazz instead") is False


def test_leftmost_play_verb_wins_over_a_higher_priority_trailing_one() -> None:
    """v3 fix: _find_play_verb_match used to pick a match by
    _PLAY_VERB_PREFIXES priority order searched across the WHOLE message,
    not by leftmost occurrence — so a later, higher-priority-listed verb
    ('play some') could beat an earlier, lower-priority one ('play'/'put
    on'), discarding the real leading command's content entirely."""
    from poob.brain.poob import _extract_play_query_span, _looks_like_non_music_play_usage

    # The real leading song survives (may still carry trailing crosstalk —
    # the comma-crosstalk trim gap is a separate, already-documented
    # follow-up — but it must no longer be DISCARDED for "games").
    span = _extract_play_query_span("Play Bohemian Rhapsody, let's play some games.")
    assert span is not None and span.lower().startswith("bohemian rhapsody")
    assert _extract_play_query_span("put on some jazz. wanna play something after") == "jazz"
    assert (
        _looks_like_non_music_play_usage("Play Bohemian Rhapsody, let's play some games.") is False
    )


def test_documented_v1_bug_reports_are_fixed() -> None:
    """Every CONFIRMED finding from the first pre-ship adversarial review,
    pinned individually so a future regex change can't silently reopen any
    of them."""
    from poob.brain.poob import _looks_like_non_music_play_usage

    f = _looks_like_non_music_play_usage
    # False positives (v1 wrongly suppressed real requests) -> must be False now.
    assert f("I want to play some jazz") is False
    assert f("let's play some tunes") is False
    assert f("wanna play some Beyonce") is False
    assert f("should I play the new Drake album") is False
    assert f("should we play some Metallica right now") is False
    assert f("you think you can play that song") is False
    # False negatives (v1 still missed paraphrases of the incident) -> now True.
    # (comma dropped vs. the original v1/v2 phrasing: v3 scopes the opinion
    # lead-in check to the clause containing the verb — see
    # test_comma_separated_opinion_anchor_is_a_documented_accepted_gap.)
    assert f("What do you reckon should we play attack or defense?") is True
    assert f("What's your take should we play hard or standard?") is True
    assert f("I think we should play it safe") is True
    # Regression (v1's unanchored whole-message match) -> leading command survives.
    assert f("Play Betty Davis eyes. Jojo Siwa. Wanna play something after") is False


def test_documented_v2_bug_reports_are_fixed() -> None:
    """Every CONFIRMED finding from the SECOND pre-ship adversarial review
    (run against v2), pinned individually so a future change can't silently
    reopen any of them. See docs/incidents/play-question-misrouted-to-play-command.md
    v3 section for the full findings."""
    from poob.brain.poob import _extract_play_query_span, _looks_like_non_music_play_usage

    f = _looks_like_non_music_play_usage
    # Finding: unrelated LEADING clause containing an opinion anchor
    # suppressed a real trailing command.
    assert f("What's your take on the new Kanye album, play Flashing Lights") is False
    # Finding: whole-message idiom search suppressed a real trailing command.
    assert f("Stop playing this, play some jazz instead") is False
    # Finding: adjective/quantifier defeated the game-reference span check.
    assert f("let's play a fun game") is True
    assert f("wanna play one more game") is True
    # Finding: priority- (not position-) ordered verb matching discarded a
    # real leading command's content.
    span = _extract_play_query_span("Play Bohemian Rhapsody, let's play some games.")
    assert span is not None and span.lower().startswith("bohemian rhapsody")
    # Finding: "round"/"match" in _GAME_REFERENCE_WORDS collided with real
    # song titles ("Round and Round").
    assert f("play round and round") is False
    # Finding: the "it cool"/"it safe" idiom prefix was defeated by one
    # interposed word.
    assert f("play it real cool") is True


def test_opinion_question_bypasses_play_backfill_entirely() -> None:
    """The exact prod regression, end to end: no tool routed, message
    contains ' play ' as a substring, but the message is an opinion
    question — must return (None, None) unchanged, NOT force a play."""
    b = _brain()
    assert b._music_safety_net(
        "Hey, Poob. Do you think we should play hard or standard?", None, None
    ) == (None, None)


def test_opinion_question_bypasses_misrouted_play_override_too() -> None:
    """Defense in depth: even if a weak rung mis-routes an opinion question
    to some OTHER music action, the misrouted-play override must not
    'correct' it into a play — the message was never a play request."""
    b = _brain()
    assert b._music_safety_net(
        "Do you think we should play hard or standard?",
        "music_assistant",
        {"action": "apply_effect", "effect": "slowed"},
    ) == ("music_assistant", {"action": "apply_effect", "effect": "slowed"})


def test_game_reference_play_still_falls_through_to_casual() -> None:
    """'let's play a game' with no tool routed must stay a no-op — the
    router (or casual fallback) handles it as conversation, not a command."""
    b = _brain()
    assert b._music_safety_net("let's play a game", None, None) == (None, None)
    assert b._music_safety_net("good play", "music_assistant", {"action": "skip"}) == (
        "music_assistant",
        {"action": "skip"},
    )


def test_misrouted_play_override_also_respects_game_reference_span() -> None:
    """The shared not-music guard call in _music_safety_net gates BOTH the
    play backfill and the misrouted-play override — pin the override branch
    specifically through the game-reference (span-content-token) check, a
    structurally different code path than the whole-message idiom regex or
    opinion lead-in already covered by
    test_opinion_question_bypasses_misrouted_play_override_too."""
    b = _brain()
    assert b._music_safety_net(
        "let's play a game", "music_assistant", {"action": "apply_effect", "effect": "slowed"}
    ) == ("music_assistant", {"action": "apply_effect", "effect": "slowed"})


def test_idiom_in_unrelated_clause_survives_end_to_end() -> None:
    """v3 fix, end to end: an idiom in an earlier clause must not swallow a
    real trailing play request when no tool was routed at all."""
    b = _brain()
    tool_name, tool_args = b._music_safety_net(
        "Stop playing this, play some jazz instead", None, None
    )
    assert tool_name == "music_assistant"
    assert tool_args["action"] == "play"
    assert "jazz" in tool_args["query"].lower()


def test_unrelated_leading_clause_survives_end_to_end() -> None:
    """v3 fix, end to end: an opinion-anchor phrase in an earlier, unrelated
    clause must not swallow a real trailing play request when no tool was
    routed at all."""
    b = _brain()
    tool_name, tool_args = b._music_safety_net(
        "What's your take on the new Kanye album, play Flashing Lights", None, None
    )
    assert tool_name == "music_assistant"
    assert tool_args["action"] == "play"
    assert "flashing lights" in tool_args["query"].lower()


def test_control_override_still_wins_over_the_not_music_guard() -> None:
    """The guard sits AFTER the control override in _music_safety_net —
    an exact bare-stop phrase like 'stop playing' still forces action=stop
    even though it also matches the not-music-play pattern."""
    b = _brain()
    assert b._music_safety_net("stop playing", None, None) == (
        "music_assistant",
        {"action": "stop"},
    )


# --- _prefer_full_play_span: router-truncation guard -------------------------
# 2026-07-16 02:06:33: "play home or let the barts out" routed query='home' →
# Edward Sharpe's "Home" played instead of the Homer meme track. The same
# utterance routed correctly 80 minutes earlier — nondeterministic truncation.


def test_prefer_full_span_extends_truncated_query() -> None:
    from poob.brain.poob import _prefer_full_play_span

    assert (
        _prefer_full_play_span("Hey Poob, play home or let the Barts out.", "home")
        == "home or let the Barts out"
    )


def test_prefer_full_span_leaves_normalized_and_exact_queries_alone() -> None:
    from poob.brain.poob import _prefer_full_play_span

    # Router normalized (not a substring) — untouched.
    assert _prefer_full_play_span("play bang bang bang a j r", "AJR BANG") == "AJR BANG"
    # Exact match — untouched.
    assert _prefer_full_play_span("play gobble glitch", "gobble glitch") == "gobble glitch"
    # No play verb in the message (query came from context legitimately).
    assert _prefer_full_play_span("that song from earlier", "despacito") == "despacito"


# --- Referential / dangling play spans (2026-07-22 prod regression) ---------
# 01:20:14  "Poob, do you wanna actually play the music that we"  (STT cut off)
#           -> span "the music that we" -> content tokens {"we"} -> NON-empty,
#           so the content-free blanking guard did NOT fire, and YouTube
#           happily matched the literal phrase: "Gorillaz - Its the music that
#           we choose". The user retried 24s later; the router's escalated rung
#           emitted query="the music that we told you to play" -> content
#           {"we","told","to"} -> junk again ("Shannon - Let The Music Play").
#
# Root cause: _PLAY_SPAN_STOPWORDS enumerated only the pronouns that appear
# when ADDRESSING poob ("can you play me...") and never the ones that appear
# when REFERRING BACK ("the music that WE asked for"). A span whose only
# survivors are function words names nothing searchable.
#
# Function words are a genuine CLOSED class — unlike the verb-phrase
# enumeration that failed three times in
# play-question-misrouted-to-play-command, this set cannot be paraphrased into
# existence. See docs/incidents/referential-play-query-searched-literally.md.


def test_referential_play_spans_carry_no_searchable_content() -> None:
    """Both production junk queues, plus close paraphrases, must reduce to
    empty content (-> 'Play what?').

    Scope is deliberately the CLOSED function-word class. A referential
    phrase that leans on an open-class verb ("what we were LISTENING to")
    still survives — extending into open-class verbs is exactly the
    unbounded enumeration that failed three times in
    play-question-misrouted-to-play-command. Not observed in production;
    extend only with evidence."""
    from poob.brain.poob import _play_span_content_tokens

    for span in (
        "the music that we",  # prod 01:20:14
        "the music that we told you to play",  # prod 01:20:39
        "the song that we asked for",
        "the songs that they asked for",
    ):
        assert _play_span_content_tokens(span) == set(), span


def test_real_song_queries_keep_their_content_tokens() -> None:
    """The closed-class extension must not swallow real requests — every
    one of these must keep at least one identifying token."""
    from poob.brain.poob import _play_span_content_tokens

    for span in (
        "jazz",
        "tiki tiki",
        "cheeky cheeky",
        "some Beyonce",
        "despacito",
        "phonk",
        "starships",
        "body dee dum",
        "John Coltrane",
        "Betty Davis eyes",
        "Bohemian Rhapsody",
        "red hot chili peppers",
        "some Metallica right now",
        "the new Drake album",
        "copper and clay by Noah Sorely",
        "Ambatakam to Marwani",
        "home or let the barts out",
        "we are young",
        "she loves you",
        "i told you so",
        "return to sender",
    ):
        assert _play_span_content_tokens(span), span


def test_referential_span_blanks_end_to_end_instead_of_queueing_junk() -> None:
    """The exact prod utterance, through the safety net: the query must be
    blanked so the empty-query gate asks 'Play what?' rather than shipping a
    dangling clause to YouTube search."""
    b = _brain()
    tool, args = b._music_safety_net(
        "Poob, do you wanna actually play the music that we", None, None
    )
    assert tool == "music_assistant"
    assert args["query"] == ""


def test_one_and_something_stay_searchable_documented_tradeoff() -> None:
    """Deliberate exclusion: 'one' and 'something' are NOT treated as
    content-free. 'One' is a real title (U2/Metallica) and poob.py already
    keeps 'one' out of the span stopwords for this reason. The cost is that
    'play the one from before' still searches literally — accepted, pinned
    here so a future change is deliberate."""
    from poob.brain.poob import _play_span_content_tokens

    assert _play_span_content_tokens("one")
    assert _play_span_content_tokens("something")


def test_the_song_still_blanks_preserving_the_census_fix() -> None:
    """Regression guard for census fix #6 ('Play the song. Fuck.' 2026-07-17)
    — generic filler must keep blanking."""
    from poob.brain.poob import _play_span_content_tokens

    assert _play_span_content_tokens("the song") == set()
    assert _play_span_content_tokens("some music") == set()


def test_one_shared_lexicon_gates_every_query_path() -> None:
    """The concept 'words that do not identify a song' must exist ONCE.
    It previously existed as three divergent copies (module-level 24 words +
    two inline 27-word duplicates), so evidence that improved one never
    reached the others — the structural reason this class of bug recurred.
    """
    import re as _re
    from pathlib import Path

    src = Path("src/poob/brain/poob.py").read_text(encoding="utf-8")
    assert "_STOPWORDS = {" not in src, (
        "an inline stopword set reappeared in poob.py — use the shared "
        "_PLAY_SPAN_STOPWORDS / _play_span_content_tokens primitives instead"
    )
    # The duplicated blocks each carried their own tokenizer; the shared
    # primitive is now the only thing that tokenizes a play span here.
    assert src.count("_play_span_content_tokens(") >= 4
    del _re


def test_all_pronoun_titles_blank_documented_tradeoff() -> None:
    """Accepted trade-off, pre-existing and unchanged in kind: a title made
    ENTIRELY of function words ("Him & I", "You and Me") reduces to empty and
    gets 'Play what?' rather than a play. "you and me"/"us"/"you" already
    behaved this way before the closed-class extension — the class is not new,
    it just now also covers subject pronouns. Pinned so a future change here
    is deliberate."""
    from poob.brain.poob import _play_span_content_tokens

    for span in ("you and me", "him and i", "us"):
        assert _play_span_content_tokens(span) == set(), span
