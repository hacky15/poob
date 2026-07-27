"""Lock the trimmed routing tool schema: stays lean, keeps load-bearing content.

MUSIC_TOOL/DEAL_TOOL are sent on EVERY routing call. The 2026-06-15 audit found
bursty VC commands blow Groq's 8000-tokens/MINUTE ceiling because each route
ships ~2.5-3k tokens, dominated by the ~1846-token MUSIC_TOOL schema. The schema
was compressed 36% (verbose per-action prose + phrasing-maps removed) WITHOUT
dropping any routing-critical guidance.

This test stops the schema from re-bloating and guards the load-bearing bits a
careless edit might drop. See docs/decisions/slim-tool-schemas.md.
"""

from __future__ import annotations

import json

import tiktoken

from poob.brain.poob import DEAL_TOOL, MUSIC_TOOL

_ENC = tiktoken.get_encoding("cl100k_base")


def _tok(obj: object) -> int:
    return len(_ENC.encode(json.dumps(obj)))


def test_music_tool_stays_under_budget() -> None:
    """Was 1846; trimmed to ~1165, budget 1350.

    **2026-07-26 re-baseline to 1400.** This assertion was RED on main from
    b7d78cf (1216 -> 1352) until the 07-26 audit found it — nothing caught it
    because CI ran no tests (see .github/workflows/test.yml, added in the same
    change). The +136 tokens are load-bearing routing guidance, not the
    verbose phrasing-maps this guard was built to keep out:
    autoplay-vs-loop disambiguation ([[autoplay-request-enables-loop-one]])
    and single-track-URL handling ([[spotify-track-link-play-dead-end]]).
    Stripping them to satisfy the old number would re-open two fixed
    incidents, so the floor moved instead — deliberately, once, with the
    reason recorded.

    The ceiling still binds: 1400 leaves ~48 tokens of headroom, so the next
    addition has to be argued for rather than slipped in. Known follow-up:
    the autoplay/loop guidance is stated in both the top-level description
    and the per-parameter descriptions; de-duplicating it is the cheap way
    back under 1350 if headroom is ever needed.
    """
    n = _tok(MUSIC_TOOL)
    assert n < 1400, f"MUSIC_TOOL re-bloated to {n} tokens (budget 1400)"


def test_deal_tool_stays_lean() -> None:
    assert _tok(DEAL_TOOL) < 260


def test_all_actions_and_params_preserved() -> None:
    """A token trim must not silently drop part of the action surface."""
    props = MUSIC_TOOL["function"]["parameters"]["properties"]
    assert set(props) == {
        "action", "query", "tracks", "value", "from_position",
        "to_position", "position", "effect", "time", "mode", "name", "url",
    }
    actions = props["action"]["enum"]
    for a in (
        "play", "queue_many", "skip", "previous", "replay", "restore",
        "pause", "resume", "stop", "volume", "volume_up", "volume_down",
        "shuffle", "loop", "now_playing", "queue", "move", "remove", "clear",
        "apply_effect", "list_effects", "seek", "autoplay", "save_playlist",
        "load_playlist", "list_playlists", "delete_playlist",
        "queue_spotify_playlist", "lyrics", "leave",
    ):
        assert a in actions, f"action {a!r} dropped from the schema"
    assert len(actions) == 30


def test_load_bearing_routing_guidance_survives() -> None:
    props = MUSIC_TOOL["function"]["parameters"]["properties"]
    # The anti-context-borrow rule (root of several mis-routes — borrowing a
    # song title from a prior turn) must survive.
    assert "borrow" in props["query"]["description"].lower()
    # The reverb ambiguity ('add reverb' usually means slowed_reverb).
    assert "slowed_reverb" in props["effect"]["description"]
    # Non-music guard (flip a coin != play a coin-flip sound).
    assert "coin" in MUSIC_TOOL["function"]["description"].lower()
    # Seek delegates format parsing to the handler.
    assert "parses" in props["time"]["description"].lower()
    # Volume extreme mapping stays.
    assert "200" in props["value"]["description"] and "mute" in props["value"]["description"].lower()
