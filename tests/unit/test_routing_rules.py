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
    assert b._music_safety_net(
        "turn off loop", "music_assistant", {"action": "loop"}
    ) == ("music_assistant", {"action": "loop", "mode": "off"})


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
    assert b._music_safety_net(
        "Hey, Poob. Max volume.", "music_assistant", {"action": "skip"}
    ) == ("music_assistant", {"action": "volume", "value": 200})


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
    assert b._music_safety_net(
        "Ben: Hey, Poob. Max volume.", None, None, voice=True
    ) == ("music_assistant", {"action": "volume", "value": 200})


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
    assert b._music_safety_net(
        "max volume", "music_assistant", {"action": "skip"}
    ) == ("music_assistant", {"action": "volume", "value": 200})


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
        assert b._music_safety_net(
            msg, "music_assistant", {"action": "play", "query": "x"}
        ) == ("music_assistant", {"action": "play", "query": "x"}), msg


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
            new=AsyncMock(return_value=("", "music_assistant", {"action": "autoplay", "mode": "on"})),
        ) as router,
        patch.object(
            b, "_casual_text_fallback", new=AsyncMock(return_value="what's up")
        ),
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
            new=AsyncMock(return_value=("", "music_assistant", {"action": "play", "query": "tiki tiki"})),
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
