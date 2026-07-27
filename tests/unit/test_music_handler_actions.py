"""Tests for the new music_assistant handler actions.

Covers the dispatch contract for: previous, replay, move, remove, clear,
queue_many, apply_effect. The existing skip/pause/volume/etc. paths are
already covered by test_music_ui.py; these tests focus on the new
branches.

Mocking strategy: AsyncYTDL.search is patched per-test to return a
crafted Track or None so we can simulate found / not-found / partial-
success cases without hitting the network. The voice client / Discord
guild objects are minimal mocks — just enough for the dispatch to
resolve the player.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from poob.discord_bot.cogs.music_cog import MusicCog
from poob.music.queue import LoopMode, MusicQueue, Track


def _t(name: str) -> Track:
    return Track(
        title=name,
        url=f"https://example.com/{name}",
        duration=timedelta(seconds=180),
    )


def _make_cog_and_player() -> tuple[MusicCog, MagicMock]:
    """Build a MusicCog with a stubbed player + ytdl + guild/member graph."""
    from poob.config import AppConfig

    bot = MagicMock()
    # Hermetic: _env_file=None keeps this off the developer's on-disk .env.
    # Without it, `discord_token` (not a real field — the field is
    # `discord_bot_token`) passed locally ONLY because .env silently supplied
    # the required values, and the test failed the moment it ran in CI.
    config = AppConfig(
        _env_file=None,
        discord_bot_token="x",
        discord_deals_channel_id=1,
        groq_api_key="x",
    )
    cog = MusicCog(bot=bot, config=config, poob_brain=None)

    # Stub the player factory to return a pre-built mock.
    player = MagicMock()
    player.queue = MagicMock()
    player.queue.size = 0
    player.queue.upcoming = []
    player.is_playing = False
    player.current_track = None
    player.active_effect = "none"
    player.set_effect = AsyncMock(return_value=None)
    player.add_effect = AsyncMock(return_value=None)
    player.remove_effect = AsyncMock(return_value=None)
    player.adjust_effect = AsyncMock(return_value=None)

    # Make _get_or_create_player return our mock regardless of args.
    cog._get_or_create_player = MagicMock(return_value=player)  # type: ignore[method-assign]

    # Stub _resolve_guild_and_player to skip the VC-membership gate.
    guild = MagicMock()
    member = MagicMock()
    member.display_name = "TestUser"
    guild.get_member.return_value = member
    vc = MagicMock()
    vc.channel = MagicMock()
    cog._resolve_guild_and_player = MagicMock(  # type: ignore[method-assign]
        return_value=(guild, vc, player, None),
    )

    return cog, player


# ---------------------------------------------------------------------------
# previous / replay
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_previous_with_empty_history_returns_silent_error() -> None:
    cog, player = _make_cog_and_player()
    player.previous = AsyncMock(return_value=None)

    resp = await cog.handle_music_request(
        "go back",
        user_id=1,
        guild_id=10,
        tool_args={"action": "previous"},
    )

    assert resp.startswith("[SILENT]")
    assert "history" in resp.lower() or "back" in resp.lower()
    player.previous.assert_awaited_once()


@pytest.mark.asyncio
async def test_previous_with_history_returns_titled_silent_reply() -> None:
    cog, player = _make_cog_and_player()
    player.previous = AsyncMock(return_value=_t("Old Song"))

    resp = await cog.handle_music_request(
        "previous",
        user_id=1,
        guild_id=10,
        tool_args={"action": "previous"},
    )

    assert resp.startswith("[SILENT]")
    assert "Old Song" in resp


@pytest.mark.asyncio
async def test_replay_with_no_current_returns_silent_error() -> None:
    cog, player = _make_cog_and_player()
    player.replay = AsyncMock(return_value=None)

    resp = await cog.handle_music_request(
        "replay",
        user_id=1,
        guild_id=10,
        tool_args={"action": "replay"},
    )

    assert resp.startswith("[SILENT]")
    assert "nothing" in resp.lower()


@pytest.mark.asyncio
async def test_replay_with_current_returns_titled_silent_reply() -> None:
    cog, player = _make_cog_and_player()
    player.replay = AsyncMock(return_value=_t("Now Playing"))

    resp = await cog.handle_music_request(
        "replay",
        user_id=1,
        guild_id=10,
        tool_args={"action": "replay"},
    )

    assert resp.startswith("[SILENT]")
    assert "Now Playing" in resp


# ---------------------------------------------------------------------------
# move / remove / clear
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_move_translates_one_based_to_zero_based() -> None:
    """User-facing queue is 1-based (matches format_queue display).
    The handler must subtract one before calling queue.move()."""
    cog, player = _make_cog_and_player()
    moved_track = _t("Track")
    player.queue.move = MagicMock(return_value=moved_track)
    player.queue.size = 3

    resp = await cog.handle_music_request(
        "move",
        user_id=1,
        guild_id=10,
        tool_args={"action": "move", "from_position": 1, "to_position": 3},
    )

    player.queue.move.assert_called_once_with(0, 2)
    assert resp.startswith("[SILENT]")
    assert "Track" in resp


@pytest.mark.asyncio
async def test_move_missing_positions_returns_silent_error() -> None:
    cog, player = _make_cog_and_player()

    resp = await cog.handle_music_request(
        "move",
        user_id=1,
        guild_id=10,
        tool_args={"action": "move"},
    )

    assert resp.startswith("[SILENT]")
    assert "from_position" in resp


@pytest.mark.asyncio
async def test_move_out_of_range_returns_silent_error() -> None:
    cog, player = _make_cog_and_player()
    player.queue.move = MagicMock(return_value=None)
    player.queue.size = 3

    resp = await cog.handle_music_request(
        "move",
        user_id=1,
        guild_id=10,
        tool_args={"action": "move", "from_position": 99, "to_position": 1},
    )

    assert resp.startswith("[SILENT]")
    assert "range" in resp.lower() or "3" in resp


@pytest.mark.asyncio
async def test_remove_translates_one_based_to_zero_based() -> None:
    cog, player = _make_cog_and_player()
    player.queue.remove = MagicMock(return_value=_t("Goner"))
    player.queue.size = 2

    resp = await cog.handle_music_request(
        "remove",
        user_id=1,
        guild_id=10,
        tool_args={"action": "remove", "position": 2},
    )

    player.queue.remove.assert_called_once_with(1)
    assert resp.startswith("[SILENT]")
    assert "Goner" in resp


@pytest.mark.asyncio
async def test_clear_returns_count_in_silent_reply() -> None:
    cog, player = _make_cog_and_player()
    player.queue.clear = MagicMock(return_value=5)

    resp = await cog.handle_music_request(
        "clear",
        user_id=1,
        guild_id=10,
        tool_args={"action": "clear"},
    )

    assert resp.startswith("[SILENT]")
    assert "5" in resp


# ---------------------------------------------------------------------------
# apply_effect
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_effect_default_mode_stacks_via_add_effect() -> None:
    # Default mode is 'add' (stack/layer) — see decisions/music-effect-stacking.
    cog, player = _make_cog_and_player()
    player.add_effect = AsyncMock(return_value="nightcore")
    player.active_effect = "nightcore"

    resp = await cog.handle_music_request(
        "nightcore it",
        user_id=1,
        guild_id=10,
        tool_args={"action": "apply_effect", "effect": "nightcore"},
    )

    player.add_effect.assert_awaited_once_with("nightcore")
    player.set_effect.assert_not_awaited()
    assert resp.startswith("[SILENT]")


@pytest.mark.asyncio
async def test_apply_effect_replace_mode_uses_set_effect() -> None:
    cog, player = _make_cog_and_player()
    player.set_effect = AsyncMock(return_value="nightcore")
    player.active_effect = "nightcore"

    resp = await cog.handle_music_request(
        "only nightcore",
        user_id=1,
        guild_id=10,
        tool_args={"action": "apply_effect", "effect": "nightcore", "mode": "replace"},
    )

    player.set_effect.assert_awaited_once_with("nightcore")
    assert resp.startswith("[SILENT]")


@pytest.mark.asyncio
async def test_apply_effect_remove_mode_uses_remove_effect() -> None:
    cog, player = _make_cog_and_player()
    player.remove_effect = AsyncMock(return_value="slowed")
    player.active_effect = "slowed"

    resp = await cog.handle_music_request(
        "take off the reverb",
        user_id=1,
        guild_id=10,
        tool_args={"action": "apply_effect", "effect": "reverb", "mode": "remove"},
    )

    player.remove_effect.assert_awaited_once_with("reverb")
    assert resp.startswith("[SILENT]")


@pytest.mark.asyncio
async def test_apply_effect_more_mode_adjusts_up() -> None:
    cog, player = _make_cog_and_player()
    player.adjust_effect = AsyncMock(return_value="heavy reverb")
    player.active_effect = "heavy reverb"

    resp = await cog.handle_music_request(
        "more reverb",
        user_id=1,
        guild_id=10,
        tool_args={"action": "apply_effect", "effect": "reverb", "mode": "more"},
    )

    player.adjust_effect.assert_awaited_once_with("reverb", "up")
    assert resp.startswith("[SILENT]")


@pytest.mark.asyncio
async def test_apply_effect_less_mode_adjusts_down() -> None:
    cog, player = _make_cog_and_player()
    player.adjust_effect = AsyncMock(return_value="bass boost")

    resp = await cog.handle_music_request(
        "less bass",
        user_id=1,
        guild_id=10,
        tool_args={"action": "apply_effect", "effect": "bass", "mode": "less"},
    )

    player.adjust_effect.assert_awaited_once_with("bass", "down")
    assert resp.startswith("[SILENT]")


@pytest.mark.asyncio
async def test_apply_effect_slower_word_adjusts_speed() -> None:
    # bare "slower"/"faster" carry their own direction — no mode needed
    cog, player = _make_cog_and_player()
    player.adjust_effect = AsyncMock(return_value="0.7x speed")

    resp = await cog.handle_music_request(
        "slower",
        user_id=1,
        guild_id=10,
        tool_args={"action": "apply_effect", "effect": "slower"},
    )

    player.adjust_effect.assert_awaited_once_with("slower")
    player.add_effect.assert_not_awaited()
    assert resp.startswith("[SILENT]")


@pytest.mark.asyncio
async def test_apply_effect_none_returns_cleared_message() -> None:
    cog, player = _make_cog_and_player()
    player.add_effect = AsyncMock(return_value=None)
    player.active_effect = "none"

    resp = await cog.handle_music_request(
        "clear the effect",
        user_id=1,
        guild_id=10,
        tool_args={"action": "apply_effect", "effect": "none"},
    )

    assert resp.startswith("[SILENT]")
    assert "clear" in resp.lower()


@pytest.mark.asyncio
async def test_apply_effect_unknown_returns_silent_error_with_valid_names() -> None:
    from poob.music.effects import EffectNotFoundError

    cog, player = _make_cog_and_player()
    player.add_effect = AsyncMock(
        side_effect=EffectNotFoundError("unknown effect 'wahwah'; valid: none, nightcore")
    )

    resp = await cog.handle_music_request(
        "wahwah",
        user_id=1,
        guild_id=10,
        tool_args={"action": "apply_effect", "effect": "wahwah"},
    )

    assert resp.startswith("[SILENT]")
    assert "wahwah" in resp


@pytest.mark.asyncio
async def test_apply_effect_no_effect_arg_returns_help() -> None:
    cog, _ = _make_cog_and_player()

    resp = await cog.handle_music_request(
        "apply an effect",
        user_id=1,
        guild_id=10,
        tool_args={"action": "apply_effect"},
    )

    assert resp.startswith("[SILENT]")
    # Should list the available presets so the user knows the surface.
    assert "nightcore" in resp.lower()
    assert "slowed" in resp.lower()


# ---------------------------------------------------------------------------
# list_effects ("what effects do you have")
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_effects_speaks_the_effect_roster() -> None:
    """'what effects do you have' must be ANSWERED aloud — NOT a silent
    control action — and the list is sourced from the registry so Poob
    never hallucinates effects he doesn't have."""
    from poob.music.effects import AVAILABLE_EFFECTS

    cog, _ = _make_cog_and_player()
    resp = await cog.handle_music_request(
        "what effects do you have",
        user_id=1,
        guild_id=10,
        tool_args={"action": "list_effects"},
    )

    assert not resp.startswith("[SILENT]")  # spoken answer, not silent
    assert "nightcore" in resp.lower()
    assert "darth vader" in resp.lower()  # underscore rendered as space
    assert "ultrabass" in resp.lower()
    # count matches the registry minus the 'none' sentinel — no drift
    expected = len([e for e in AVAILABLE_EFFECTS if e != "none"])
    assert str(expected) in resp


def test_music_tool_schema_advertises_list_effects() -> None:
    from poob.brain.poob import MUSIC_TOOL

    enum = MUSIC_TOOL["function"]["parameters"]["properties"]["action"]["enum"]
    assert "list_effects" in enum


# ---------------------------------------------------------------------------
# restore ("put the music back on") — 2026-06-11
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_restore_dispatches_to_player_restore_last() -> None:
    cog, player = _make_cog_and_player()
    player.restore_last = AsyncMock(return_value=_t("Last Song"))

    resp = await cog.handle_music_request(
        "put the music back on",
        user_id=1,
        guild_id=10,
        tool_args={"action": "restore"},
    )

    player.restore_last.assert_awaited_once()
    assert resp.startswith("[SILENT]")
    assert "Last Song" in resp


@pytest.mark.asyncio
async def test_restore_nothing_to_bring_back_is_graceful() -> None:
    cog, player = _make_cog_and_player()
    player.restore_last = AsyncMock(return_value=None)

    resp = await cog.handle_music_request(
        "put the music back on",
        user_id=1,
        guild_id=10,
        tool_args={"action": "restore"},
    )

    assert resp.startswith("[SILENT]")
    assert "nothing" in resp.lower()


def test_music_tool_schema_advertises_restore() -> None:
    from poob.brain.poob import MUSIC_TOOL

    enum = MUSIC_TOOL["function"]["parameters"]["properties"]["action"]["enum"]
    assert "restore" in enum


# ---------------------------------------------------------------------------
# queue_many
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_queue_many_all_resolve_reports_count() -> None:
    cog, player = _make_cog_and_player()
    player.play = AsyncMock()
    cog._ytdl = MagicMock()
    cog._ytdl.search = AsyncMock(
        side_effect=[
            _t("Bohemian Rhapsody"),
            _t("Don't Stop Believin'"),
            _t("Africa"),
        ]
    )
    player.is_playing = False

    resp = await cog.handle_music_request(
        "play three songs",
        user_id=1,
        guild_id=10,
        tool_args={
            "action": "queue_many",
            "tracks": ["Bohemian Rhapsody", "Don't Stop Believin'", "Africa by Toto"],
        },
    )

    assert player.play.await_count == 3
    assert "3" in resp or "queued" in resp.lower() or "playing" in resp.lower()


@pytest.mark.asyncio
async def test_queue_many_partial_resolution_reports_misses() -> None:
    cog, player = _make_cog_and_player()
    player.play = AsyncMock()
    cog._ytdl = MagicMock()
    cog._ytdl.search = AsyncMock(
        side_effect=[
            _t("Bohemian Rhapsody"),
            None,
            _t("Africa"),
        ]
    )
    player.is_playing = True

    resp = await cog.handle_music_request(
        "queue stuff",
        user_id=1,
        guild_id=10,
        tool_args={
            "action": "queue_many",
            "tracks": [
                "Bohemian Rhapsody",
                "Made-up Song That Won't Resolve",
                "Africa by Toto",
            ],
        },
    )

    assert player.play.await_count == 2
    assert "couldn't find" in resp.lower() or "made-up" in resp.lower()


@pytest.mark.asyncio
async def test_queue_many_all_misses_returns_couldnt_find() -> None:
    cog, player = _make_cog_and_player()
    player.play = AsyncMock()
    cog._ytdl = MagicMock()
    cog._ytdl.search = AsyncMock(return_value=None)

    resp = await cog.handle_music_request(
        "queue stuff",
        user_id=1,
        guild_id=10,
        tool_args={"action": "queue_many", "tracks": ["x", "y", "z"]},
    )

    player.play.assert_not_called()
    assert "couldn't find" in resp.lower()


@pytest.mark.asyncio
async def test_queue_many_empty_list_returns_silent_error() -> None:
    cog, player = _make_cog_and_player()

    resp = await cog.handle_music_request(
        "queue many",
        user_id=1,
        guild_id=10,
        tool_args={"action": "queue_many", "tracks": []},
    )

    assert resp.startswith("[SILENT]")
    assert "non-empty" in resp.lower() or "tracks" in resp.lower()


@pytest.mark.asyncio
async def test_queue_many_drops_too_short_titles() -> None:
    """STT can emit single-letter noise like 'a' or empty strings.
    Don't search for those — count them as not-found."""
    cog, player = _make_cog_and_player()
    player.play = AsyncMock()
    cog._ytdl = MagicMock()
    cog._ytdl.search = AsyncMock(return_value=_t("OK Song"))

    resp = await cog.handle_music_request(
        "queue many",
        user_id=1,
        guild_id=10,
        tool_args={"action": "queue_many", "tracks": ["a", "", "OK Song"]},
    )

    # Only "OK Song" was long enough to be searched.
    assert cog._ytdl.search.await_count == 1
    assert player.play.await_count == 1


# ---------------------------------------------------------------------------
# Tool schema regression guard
# ---------------------------------------------------------------------------


def test_music_tool_schema_advertises_all_new_actions() -> None:
    from poob.brain.poob import MUSIC_TOOL

    enum = MUSIC_TOOL["function"]["parameters"]["properties"]["action"]["enum"]
    for action in (
        "previous",
        "replay",
        "move",
        "remove",
        "clear",
        "queue_many",
        "apply_effect",
        "seek",
    ):
        assert action in enum, f"MUSIC_TOOL missing {action!r}"


def test_music_tool_schema_declares_new_parameters() -> None:
    from poob.brain.poob import MUSIC_TOOL

    props = MUSIC_TOOL["function"]["parameters"]["properties"]
    for param in (
        "tracks",
        "from_position",
        "to_position",
        "position",
        "effect",
        "time",
    ):
        assert param in props, f"MUSIC_TOOL missing {param!r} parameter"

    # ``tracks`` must be an array of strings — pattern (a) from the
    # roadmap. Anything else (e.g. a single string with commas) would
    # force server-side parsing.
    assert props["tracks"]["type"] == "array"
    assert props["tracks"]["items"]["type"] == "string"


# ---------------------------------------------------------------------------
# seek
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_seek_dispatches_to_player_with_parsed_input() -> None:
    cog, player = _make_cog_and_player()
    track = _t("Now Playing")
    player.seek = AsyncMock(return_value=(track, 150.0))

    resp = await cog.handle_music_request(
        "seek to two thirty",
        user_id=1,
        guild_id=10,
        tool_args={"action": "seek", "time": "2:30"},
    )

    # The handler passes a ParsedSeek to player.seek.
    assert player.seek.await_count == 1
    parsed_arg = player.seek.await_args.args[0]
    from poob.music.seek import ParsedSeek

    assert isinstance(parsed_arg, ParsedSeek)
    assert parsed_arg.seconds == 150.0
    assert parsed_arg.relative is False

    assert resp.startswith("[SILENT]")
    assert "2:30" in resp or "Now Playing" in resp


@pytest.mark.asyncio
async def test_seek_relative_format_dispatches_with_relative_flag() -> None:
    cog, player = _make_cog_and_player()
    track = _t("Track")
    player.seek = AsyncMock(return_value=(track, 60.0))

    await cog.handle_music_request(
        "skip ahead",
        user_id=1,
        guild_id=10,
        tool_args={"action": "seek", "time": "+10s"},
    )

    parsed_arg = player.seek.await_args.args[0]
    assert parsed_arg.seconds == 10.0
    assert parsed_arg.relative is True


@pytest.mark.asyncio
async def test_seek_missing_time_arg_returns_silent_prompt() -> None:
    cog, player = _make_cog_and_player()

    resp = await cog.handle_music_request(
        "seek",
        user_id=1,
        guild_id=10,
        tool_args={"action": "seek"},
    )

    assert resp.startswith("[SILENT]")
    assert "2:30" in resp or "+10" in resp  # format hint in the prompt


@pytest.mark.asyncio
async def test_seek_invalid_format_returns_silent_parse_error() -> None:
    cog, player = _make_cog_and_player()

    resp = await cog.handle_music_request(
        "seek to the future",
        user_id=1,
        guild_id=10,
        tool_args={"action": "seek", "time": "the future"},
    )

    assert resp.startswith("[SILENT]")
    assert "future" in resp


@pytest.mark.asyncio
async def test_seek_with_no_current_track_returns_silent_error() -> None:
    cog, player = _make_cog_and_player()
    player.seek = AsyncMock(return_value=None)
    player.current_track = None

    resp = await cog.handle_music_request(
        "seek",
        user_id=1,
        guild_id=10,
        tool_args={"action": "seek", "time": "1:00"},
    )

    assert resp.startswith("[SILENT]")
    assert "nothing" in resp.lower() or "playing" in resp.lower()


# ---------------------------------------------------------------------------
# leave
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_leave_stops_player_then_calls_voice_cog_force_disconnect() -> None:
    """Leave is cross-cog: MusicCog handler reaches for VoiceCog's
    force-disconnect path. The order must be stop-music-first (so the
    audio thread exits cleanly), then disconnect the VC."""
    cog, player = _make_cog_and_player()
    player.stop = AsyncMock()
    voice_cog = MagicMock()
    voice_cog._force_disconnect = AsyncMock()
    cog.bot = MagicMock()
    cog.bot.get_cog = MagicMock(return_value=voice_cog)

    resp = await cog.handle_music_request(
        "leave",
        user_id=1,
        guild_id=10,
        tool_args={"action": "leave"},
    )

    player.stop.assert_awaited_once()
    voice_cog._force_disconnect.assert_awaited_once()
    assert resp.startswith("[SILENT]")
    assert "left" in resp.lower() or "voice" in resp.lower()


@pytest.mark.asyncio
async def test_leave_handles_missing_voice_cog_gracefully() -> None:
    """If VoiceCog isn't loaded (music_enabled but voice_enabled=False
    won't actually happen due to the wiring, but defensive code matters):
    don't crash, just stop the music and report success."""
    cog, player = _make_cog_and_player()
    player.stop = AsyncMock()
    cog.bot = MagicMock()
    cog.bot.get_cog = MagicMock(return_value=None)

    resp = await cog.handle_music_request(
        "leave",
        user_id=1,
        guild_id=10,
        tool_args={"action": "leave"},
    )

    player.stop.assert_awaited_once()
    assert resp.startswith("[SILENT]")


@pytest.mark.asyncio
async def test_seek_on_livestream_returns_silent_error_with_stream_message() -> None:
    cog, player = _make_cog_and_player()
    player.seek = AsyncMock(return_value=None)
    stream_track = Track(title="LiveStream", url="x", duration=None, is_stream=True)
    player.current_track = stream_track

    resp = await cog.handle_music_request(
        "seek",
        user_id=1,
        guild_id=10,
        tool_args={"action": "seek", "time": "1:00"},
    )

    assert resp.startswith("[SILENT]")
    # Surfaces the stream-specific message so user knows why it failed.
    assert "stream" in resp.lower() or "live" in resp.lower()


# ---------------------------------------------------------------------------
# autoplay action dispatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_autoplay_on_enables_and_confirms() -> None:
    cog, player = _make_cog_and_player()
    player.autoplay_enabled = False

    resp = await cog.handle_music_request(
        "autoplay on",
        user_id=1,
        guild_id=10,
        tool_args={"action": "autoplay", "mode": "on"},
    )

    assert resp.startswith("[SILENT]")
    assert "enabled" in resp.lower()
    assert player.autoplay_enabled is True


@pytest.mark.asyncio
async def test_autoplay_off_disables_and_confirms() -> None:
    cog, player = _make_cog_and_player()
    player.autoplay_enabled = True

    resp = await cog.handle_music_request(
        "autoplay off",
        user_id=1,
        guild_id=10,
        tool_args={"action": "autoplay", "mode": "off"},
    )

    assert resp.startswith("[SILENT]")
    assert "disabled" in resp.lower()
    assert player.autoplay_enabled is False


@pytest.mark.asyncio
async def test_autoplay_status_reports_current_state() -> None:
    cog, player = _make_cog_and_player()
    player.autoplay_enabled = True

    resp = await cog.handle_music_request(
        "autoplay?",
        user_id=1,
        guild_id=10,
        tool_args={"action": "autoplay", "mode": "status"},
    )

    assert resp.startswith("[SILENT]")
    assert " on" in resp.lower() or "on." in resp.lower()
    # Status MUST NOT mutate state.
    assert player.autoplay_enabled is True


@pytest.mark.asyncio
async def test_autoplay_missing_mode_returns_help() -> None:
    cog, player = _make_cog_and_player()
    player.autoplay_enabled = False

    resp = await cog.handle_music_request(
        "autoplay",
        user_id=1,
        guild_id=10,
        tool_args={"action": "autoplay"},  # no mode field
    )

    assert resp.startswith("[SILENT]")
    assert "mode" in resp.lower() or "on" in resp.lower()
    assert player.autoplay_enabled is False  # untouched on bad input


# ---------------------------------------------------------------------------
# loop action dispatch — honor the explicit target, cycle only on bare toggle.
#
# Regression: the handler used to call cycle_loop_mode() unconditionally,
# discarding the mode/value the router passed. "turn loop off" then CYCLED
# (OFF -> LOOP_ONE), so loop could never be turned off and a misrouted
# "autoplay on" enabled LOOP_ONE — replaying the same song forever. The 🔁
# button still sends {action: loop} with no mode and MUST keep cycling.
# See docs/incidents/autoplay-request-enables-loop-one.md.
# ---------------------------------------------------------------------------


def _make_cog_and_real_queue() -> tuple[MusicCog, MagicMock]:
    """Like _make_cog_and_player but with a REAL MusicQueue so loop-mode
    transitions can be asserted for real (not a MagicMock stand-in)."""
    cog, player = _make_cog_and_player()
    player.queue = MusicQueue()
    return cog, player


@pytest.mark.asyncio
async def test_loop_off_sets_off_from_loop_one() -> None:
    cog, player = _make_cog_and_real_queue()
    player.queue.set_loop_mode(LoopMode.LOOP_ONE)

    resp = await cog.handle_music_request(
        "turn off loop",
        user_id=1,
        guild_id=10,
        tool_args={"action": "loop", "mode": "off"},
    )

    assert resp.startswith("[SILENT]")
    assert "off" in resp.lower()
    assert player.queue.loop_mode is LoopMode.OFF


@pytest.mark.asyncio
async def test_loop_off_stays_off_when_already_off() -> None:
    """The exact prod bug: "loop off" while already OFF must NOT cycle to
    LOOP_ONE. Honoring the target makes this idempotent."""
    cog, player = _make_cog_and_real_queue()
    assert player.queue.loop_mode is LoopMode.OFF

    resp = await cog.handle_music_request(
        "turn off loop",
        user_id=1,
        guild_id=10,
        tool_args={"action": "loop", "mode": "off"},
    )

    assert player.queue.loop_mode is LoopMode.OFF
    assert "off" in resp.lower()


@pytest.mark.asyncio
async def test_loop_one_sets_loop_one() -> None:
    cog, player = _make_cog_and_real_queue()

    resp = await cog.handle_music_request(
        "loop this song",
        user_id=1,
        guild_id=10,
        tool_args={"action": "loop", "mode": "one"},
    )

    assert player.queue.loop_mode is LoopMode.LOOP_ONE
    assert "current" in resp.lower() or "track" in resp.lower()


@pytest.mark.asyncio
async def test_loop_queue_sets_loop_queue() -> None:
    cog, player = _make_cog_and_real_queue()

    resp = await cog.handle_music_request(
        "loop the whole queue",
        user_id=1,
        guild_id=10,
        tool_args={"action": "loop", "mode": "queue"},
    )

    assert player.queue.loop_mode is LoopMode.LOOP_QUEUE
    assert "queue" in resp.lower()


@pytest.mark.asyncio
async def test_loop_target_accepted_via_value_field() -> None:
    """STT/model sometimes packs the target in 'value' ({loop, value: 'off'},
    observed in prod). Read value as a fallback for mode."""
    cog, player = _make_cog_and_real_queue()
    player.queue.set_loop_mode(LoopMode.LOOP_ONE)

    resp = await cog.handle_music_request(
        "loop off",
        user_id=1,
        guild_id=10,
        tool_args={"action": "loop", "value": "off"},
    )

    assert player.queue.loop_mode is LoopMode.OFF
    assert "off" in resp.lower()


@pytest.mark.asyncio
async def test_loop_bare_toggle_cycles_for_button() -> None:
    """The 🔁 button sends {action: loop} with NO mode -> must CYCLE
    OFF -> LOOP_ONE -> LOOP_QUEUE -> OFF (behavior preserved)."""
    cog, player = _make_cog_and_real_queue()
    assert player.queue.loop_mode is LoopMode.OFF

    await cog.handle_music_request("", 1, 10, tool_args={"action": "loop"})
    assert player.queue.loop_mode is LoopMode.LOOP_ONE
    await cog.handle_music_request("", 1, 10, tool_args={"action": "loop"})
    assert player.queue.loop_mode is LoopMode.LOOP_QUEUE
    await cog.handle_music_request("", 1, 10, tool_args={"action": "loop"})
    assert player.queue.loop_mode is LoopMode.OFF


@pytest.mark.asyncio
async def test_loop_status_reports_without_mutating() -> None:
    cog, player = _make_cog_and_real_queue()
    player.queue.set_loop_mode(LoopMode.LOOP_QUEUE)

    resp = await cog.handle_music_request(
        "what's the loop mode",
        user_id=1,
        guild_id=10,
        tool_args={"action": "loop", "mode": "status"},
    )

    assert player.queue.loop_mode is LoopMode.LOOP_QUEUE  # unchanged
    assert "queue" in resp.lower()


@pytest.mark.asyncio
async def test_loop_unrecognized_mode_falls_back_to_cycle() -> None:
    """A garbage/effect-flavored value must not crash — fall back to the
    historical cycle behavior rather than raising."""
    cog, player = _make_cog_and_real_queue()
    assert player.queue.loop_mode is LoopMode.OFF

    resp = await cog.handle_music_request(
        "loop",
        user_id=1,
        guild_id=10,
        tool_args={"action": "loop", "mode": "add"},
    )

    assert resp.startswith("[SILENT]")
    assert player.queue.loop_mode is LoopMode.LOOP_ONE  # cycled OFF -> ONE


def test_music_tool_schema_mode_supports_loop_targets() -> None:
    from poob.brain.poob import MUSIC_TOOL

    mode = MUSIC_TOOL["function"]["parameters"]["properties"]["mode"]
    assert "one" in mode["enum"]
    assert "queue" in mode["enum"]
    assert "loop" in mode["description"].lower()


# ---------------------------------------------------------------------------
# play: pasted links — url-arg fallback + Spotify track/playlist resolution.
# 2026-07-11 prod: a pasted Spotify track link routed to {play, url:...} and
# died in "What do you want me to play?" ("Yeah. It didn't work.").
# See docs/incidents/spotify-track-link-play-dead-end.md.
# ---------------------------------------------------------------------------


def _t_short(name: str) -> Track:
    return Track(title=name, url=f"https://example.com/{name}", duration=timedelta(seconds=200))


def _wire_same_vc(cog: MusicCog) -> None:
    """Put the mock requester in the same VC as the bot.

    The 'play' fall-through (unlike named control actions) enforces the
    same-VC gate; two distinct MagicMocks compare unequal, so the requester
    must share the exact channel object with the voice client."""
    guild = cog.bot.get_guild.return_value
    guild.voice_client.is_connected.return_value = True
    guild.get_member.return_value.voice.channel = guild.voice_client.channel


@pytest.mark.asyncio
async def test_play_uses_url_arg_when_query_missing() -> None:
    """A YouTube link the router put in 'url' plays as if it were the query."""
    cog, player = _make_cog_and_player()
    _wire_same_vc(cog)
    player.play = AsyncMock()
    cog._ytdl = MagicMock()
    cog._ytdl.search = AsyncMock(return_value=_t_short("Linked Song"))

    resp = await cog.handle_music_request(
        "play this",
        user_id=1,
        guild_id=10,
        tool_args={"action": "play", "url": "https://youtu.be/abc123"},
    )

    cog._ytdl.search.assert_awaited_once()
    assert cog._ytdl.search.await_args.args[0] == "https://youtu.be/abc123"
    assert "Linked Song" in resp


@pytest.mark.asyncio
async def test_play_spotify_track_link_resolves_metadata_then_searches() -> None:
    """Spotify track link → title+artist via resolver → normal YT search."""
    cog, player = _make_cog_and_player()
    _wire_same_vc(cog)
    player.play = AsyncMock()
    cog._ytdl = MagicMock()
    cog._ytdl.search = AsyncMock(return_value=_t_short("The Heavy - Short Change Hero"))
    resolver = MagicMock()
    resolver.is_configured.return_value = True
    resolver.resolve_track = AsyncMock(
        return_value={"title": "Short Change Hero", "artist": "The Heavy"}
    )
    cog._spotify_resolver = resolver

    resp = await cog.handle_music_request(
        "play this",
        user_id=1,
        guild_id=10,
        tool_args={
            "action": "play",
            "query": "https://open.spotify.com/track/0gfkjiPutU79nqcPbcR1NR?si=x",
        },
    )

    resolver.resolve_track.assert_awaited_once()
    assert cog._ytdl.search.await_args.args[0] == "Short Change Hero The Heavy"
    assert "Short Change Hero" in resp


@pytest.mark.asyncio
async def test_play_spotify_track_link_unconfigured_gives_honest_error() -> None:
    cog, player = _make_cog_and_player()
    _wire_same_vc(cog)
    cog._ytdl = MagicMock()
    cog._ytdl.search = AsyncMock()
    cog._spotify_resolver = None

    resp = await cog.handle_music_request(
        "play this",
        user_id=1,
        guild_id=10,
        tool_args={"action": "play", "query": "spotify:track:abc123"},
    )

    cog._ytdl.search.assert_not_called()
    assert "spotify" in resp.lower()
    assert "song name" in resp.lower()


@pytest.mark.asyncio
async def test_play_spotify_track_resolve_failure_gives_honest_error() -> None:
    cog, player = _make_cog_and_player()
    _wire_same_vc(cog)
    cog._ytdl = MagicMock()
    cog._ytdl.search = AsyncMock()
    resolver = MagicMock()
    resolver.is_configured.return_value = True
    resolver.resolve_track = AsyncMock(return_value=None)
    cog._spotify_resolver = resolver

    resp = await cog.handle_music_request(
        "play this",
        user_id=1,
        guild_id=10,
        tool_args={"action": "play", "query": "spotify:track:abc123"},
    )

    cog._ytdl.search.assert_not_called()
    assert "couldn't read" in resp.lower() or "song name" in resp.lower()


@pytest.mark.asyncio
async def test_play_spotify_playlist_link_routes_to_spotify_flow() -> None:
    """A Spotify PLAYLIST link pasted with 'play' must go through the
    Spotify resolver flow — NOT the YouTube playlist extractor (yt-dlp
    can't read Spotify; the '/playlist' substring check would trap it)."""
    cog, player = _make_cog_and_player()
    _wire_same_vc(cog)
    player.play = AsyncMock()
    cog._ytdl = MagicMock()
    cog._ytdl.get_playlist_tracks = AsyncMock()
    cog._ytdl.search = AsyncMock(return_value=_t_short("Song A"))
    resolver = MagicMock()
    resolver.is_configured.return_value = True
    resolver.resolve = AsyncMock(return_value=[{"title": "Song A", "artist": "Artist"}])
    cog._spotify_resolver = resolver

    resp = await cog.handle_music_request(
        "play this playlist",
        user_id=1,
        guild_id=10,
        tool_args={
            "action": "play",
            "query": "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M",
        },
    )

    resolver.resolve.assert_awaited_once()
    cog._ytdl.get_playlist_tracks.assert_not_called()
    assert "Song A" in resp


@pytest.mark.asyncio
async def test_queue_spotify_playlist_action_still_works_via_shared_helper() -> None:
    """The extracted helper serves the original action unchanged."""
    cog, player = _make_cog_and_player()
    player.play = AsyncMock()
    player.is_playing = False
    cog._ytdl = MagicMock()
    cog._ytdl.search = AsyncMock(
        side_effect=[_t_short("Song A"), _t_short("Song B")]
    )
    resolver = MagicMock()
    resolver.is_configured.return_value = True
    resolver.resolve = AsyncMock(
        return_value=[
            {"title": "Song A", "artist": "X"},
            {"title": "Song B", "artist": "Y"},
        ]
    )
    cog._spotify_resolver = resolver

    resp = await cog.handle_music_request(
        "queue my playlist",
        user_id=1,
        guild_id=10,
        tool_args={
            "action": "queue_spotify_playlist",
            "url": "https://open.spotify.com/playlist/abc",
        },
    )

    assert player.play.await_count == 2
    assert "Song A" in resp


# ---------------------------------------------------------------------------
# Named playlists: save / load / list / delete
# ---------------------------------------------------------------------------


def _make_cog_with_playlist_repo() -> tuple[MusicCog, MagicMock, MagicMock]:
    """Build a MusicCog with a stubbed player AND a mocked playlist repo.

    Returns ``(cog, player, repo)`` so tests can drive repo behavior via
    ``AsyncMock`` returns and assert ``.assert_awaited_with(...)`` after
    the dispatch lands.
    """
    cog, player = _make_cog_and_player()

    repo = MagicMock()
    repo.save = AsyncMock(return_value=None)
    repo.load = AsyncMock(return_value=None)
    repo.list_names = AsyncMock(return_value=[])
    repo.delete = AsyncMock(return_value=True)
    cog._playlist_repo = repo  # type: ignore[attr-defined]
    return cog, player, repo


@pytest.mark.asyncio
async def test_save_playlist_with_queue_persists_and_replies() -> None:
    cog, player, repo = _make_cog_with_playlist_repo()
    player.current_track = _t("Current Song")
    player.queue.upcoming = [_t("Up Next 1"), _t("Up Next 2")]
    repo.load = AsyncMock(return_value=None)  # name does not exist yet

    resp = await cog.handle_music_request(
        "save as chill",
        user_id=1,
        guild_id=10,
        tool_args={"action": "save_playlist", "name": "chill"},
    )

    assert resp.startswith("[SILENT]")
    assert "Saved" in resp
    assert "chill" in resp
    repo.save.assert_awaited_once()
    args, _ = repo.save.call_args
    saved_guild, saved_name, saved_tracks = args
    assert saved_name == "chill"
    assert len(saved_tracks) == 3  # current + 2 upcoming


@pytest.mark.asyncio
async def test_save_playlist_with_empty_queue_rejects() -> None:
    cog, player, repo = _make_cog_with_playlist_repo()
    player.current_track = None
    player.queue.upcoming = []

    resp = await cog.handle_music_request(
        "save as chill",
        user_id=1,
        guild_id=10,
        tool_args={"action": "save_playlist", "name": "chill"},
    )

    assert resp.startswith("[SILENT]")
    assert "empty" in resp.lower() or "nothing" in resp.lower()
    repo.save.assert_not_awaited()


@pytest.mark.asyncio
async def test_save_playlist_existing_name_says_updated() -> None:
    cog, player, repo = _make_cog_with_playlist_repo()
    player.current_track = _t("A")
    player.queue.upcoming = []
    repo.load = AsyncMock(return_value=[{"title": "old"}])  # name exists

    resp = await cog.handle_music_request(
        "save",
        user_id=1,
        guild_id=10,
        tool_args={"action": "save_playlist", "name": "chill"},
    )

    assert "Updated" in resp
    assert "chill" in resp
    repo.save.assert_awaited_once()


@pytest.mark.asyncio
async def test_save_playlist_missing_name_returns_help() -> None:
    cog, player, repo = _make_cog_with_playlist_repo()

    resp = await cog.handle_music_request(
        "save",
        user_id=1,
        guild_id=10,
        tool_args={"action": "save_playlist"},  # no name
    )

    assert resp.startswith("[SILENT]")
    assert "name" in resp.lower()
    repo.save.assert_not_awaited()


@pytest.mark.asyncio
async def test_load_playlist_appends_to_queue() -> None:
    cog, player, repo = _make_cog_with_playlist_repo()
    saved_tracks = [
        {
            "title": "T1",
            "url": "u1",
            "identifier": "i1",
            "duration_seconds": 60,
            "source": "youtube",
            "is_stream": False,
        },
        {
            "title": "T2",
            "url": "u2",
            "identifier": "i2",
            "duration_seconds": 90,
            "source": "youtube",
            "is_stream": False,
        },
        {
            "title": "T3",
            "url": "u3",
            "identifier": "i3",
            "duration_seconds": 120,
            "source": "youtube",
            "is_stream": False,
        },
    ]
    repo.load = AsyncMock(return_value=saved_tracks)

    resp = await cog.handle_music_request(
        "load chill",
        user_id=1,
        guild_id=10,
        tool_args={"action": "load_playlist", "name": "chill"},
    )

    assert resp.startswith("[SILENT]")
    assert "Loaded" in resp
    assert "3 tracks" in resp
    # queue.add was called once per track
    assert player.queue.add.call_count == 3


@pytest.mark.asyncio
async def test_load_playlist_missing_returns_silent_error() -> None:
    cog, player, repo = _make_cog_with_playlist_repo()
    repo.load = AsyncMock(return_value=None)

    resp = await cog.handle_music_request(
        "load chill",
        user_id=1,
        guild_id=10,
        tool_args={"action": "load_playlist", "name": "chill"},
    )

    assert resp.startswith("[SILENT]")
    assert "no playlist" in resp.lower()
    player.queue.add.assert_not_called()


@pytest.mark.asyncio
async def test_load_playlist_missing_name_returns_help() -> None:
    cog, player, repo = _make_cog_with_playlist_repo()

    resp = await cog.handle_music_request(
        "load",
        user_id=1,
        guild_id=10,
        tool_args={"action": "load_playlist"},
    )

    assert "name" in resp.lower()
    repo.load.assert_not_awaited()


@pytest.mark.asyncio
async def test_list_playlists_returns_alphabetical_names() -> None:
    cog, player, repo = _make_cog_with_playlist_repo()
    repo.list_names = AsyncMock(return_value=["chill", "deep-focus", "gym"])

    resp = await cog.handle_music_request(
        "list my playlists",
        user_id=1,
        guild_id=10,
        tool_args={"action": "list_playlists"},
    )

    assert resp.startswith("[SILENT]")
    assert "chill" in resp
    assert "deep-focus" in resp
    assert "gym" in resp
    repo.list_names.assert_awaited_once()


@pytest.mark.asyncio
async def test_list_playlists_empty_returns_friendly_message() -> None:
    cog, player, repo = _make_cog_with_playlist_repo()
    repo.list_names = AsyncMock(return_value=[])

    resp = await cog.handle_music_request(
        "list",
        user_id=1,
        guild_id=10,
        tool_args={"action": "list_playlists"},
    )

    assert resp.startswith("[SILENT]")
    assert "no saved" in resp.lower() or "no playlists" in resp.lower()


@pytest.mark.asyncio
async def test_delete_playlist_removes_and_replies() -> None:
    cog, player, repo = _make_cog_with_playlist_repo()
    repo.delete = AsyncMock(return_value=True)

    resp = await cog.handle_music_request(
        "forget chill",
        user_id=1,
        guild_id=10,
        tool_args={"action": "delete_playlist", "name": "chill"},
    )

    assert resp.startswith("[SILENT]")
    assert "Deleted" in resp
    assert "chill" in resp


@pytest.mark.asyncio
async def test_delete_playlist_missing_returns_silent_error() -> None:
    cog, player, repo = _make_cog_with_playlist_repo()
    repo.delete = AsyncMock(return_value=False)

    resp = await cog.handle_music_request(
        "forget nonexistent",
        user_id=1,
        guild_id=10,
        tool_args={"action": "delete_playlist", "name": "nonexistent"},
    )

    assert resp.startswith("[SILENT]")
    assert "no playlist" in resp.lower()


@pytest.mark.asyncio
async def test_delete_playlist_missing_name_returns_help() -> None:
    cog, player, repo = _make_cog_with_playlist_repo()

    resp = await cog.handle_music_request(
        "delete",
        user_id=1,
        guild_id=10,
        tool_args={"action": "delete_playlist"},
    )

    assert "name" in resp.lower()
    repo.delete.assert_not_awaited()


# ---------------------------------------------------------------------------
# Spotify playlist URL import (queue_spotify_playlist)
# ---------------------------------------------------------------------------


def _make_cog_with_spotify(
    is_configured: bool = True,
    resolve_return=None,
) -> tuple[MusicCog, MagicMock, MagicMock]:
    """Cog + player + mocked Spotify resolver."""
    cog, player = _make_cog_and_player()

    resolver = MagicMock()
    resolver.is_configured = MagicMock(return_value=is_configured)
    resolver.resolve = AsyncMock(return_value=resolve_return)
    cog._spotify_resolver = resolver  # type: ignore[attr-defined]
    return cog, player, resolver


@pytest.mark.asyncio
async def test_queue_spotify_playlist_not_configured_returns_soft_error() -> None:
    cog, player, resolver = _make_cog_with_spotify(is_configured=False)

    resp = await cog.handle_music_request(
        "queue this",
        user_id=1,
        guild_id=10,
        tool_args={
            "action": "queue_spotify_playlist",
            "url": "https://open.spotify.com/playlist/abc123",
        },
    )

    assert resp.startswith("[SILENT]")
    assert "spotify" in resp.lower()
    resolver.resolve.assert_not_awaited()


@pytest.mark.asyncio
async def test_queue_spotify_playlist_missing_url_returns_help() -> None:
    cog, player, resolver = _make_cog_with_spotify(is_configured=True)

    resp = await cog.handle_music_request(
        "queue spotify",
        user_id=1,
        guild_id=10,
        tool_args={"action": "queue_spotify_playlist"},
    )

    assert resp.startswith("[SILENT]")
    assert "url" in resp.lower() or "playlist" in resp.lower()
    resolver.resolve.assert_not_awaited()


@pytest.mark.asyncio
async def test_queue_spotify_playlist_resolves_and_queues() -> None:
    resolved_titles = [
        {"title": "Track A", "artist": "Artist 1"},
        {"title": "Track B", "artist": "Artist 2"},
        {"title": "Track C", "artist": "Artist 3"},
    ]
    cog, player, resolver = _make_cog_with_spotify(
        is_configured=True,
        resolve_return=resolved_titles,
    )
    # Music already playing → response uses the "Queued N from Spotify" form
    player.is_playing = True
    cog._ytdl = MagicMock()  # type: ignore[attr-defined]
    cog._ytdl.search = AsyncMock(
        side_effect=[
            _t("Track A - Artist 1"),
            _t("Track B - Artist 2"),
            _t("Track C - Artist 3"),
        ]
    )
    player.play = AsyncMock(return_value=None)

    resp = await cog.handle_music_request(
        "queue this spotify playlist",
        user_id=1,
        guild_id=10,
        tool_args={
            "action": "queue_spotify_playlist",
            "url": "https://open.spotify.com/playlist/abc123",
        },
    )

    assert resp.startswith("[SILENT]")
    assert "Queued 3" in resp
    assert "Spotify" in resp
    assert cog._ytdl.search.await_count == 3
    assert player.play.await_count == 3


@pytest.mark.asyncio
async def test_queue_spotify_playlist_partial_resolution_reports_not_found() -> None:
    resolved_titles = [
        {"title": "Track A", "artist": "Artist 1"},
        {"title": "Track B", "artist": "Artist 2"},
        {"title": "Track C", "artist": "Artist 3"},
    ]
    cog, player, resolver = _make_cog_with_spotify(
        is_configured=True,
        resolve_return=resolved_titles,
    )
    # Music already playing → response uses the "Queued N from Spotify (M not found)" form
    player.is_playing = True
    cog._ytdl = MagicMock()  # type: ignore[attr-defined]
    cog._ytdl.search = AsyncMock(
        side_effect=[
            _t("Track A - Artist 1"),
            None,  # second track unresolvable on YouTube
            _t("Track C - Artist 3"),
        ]
    )
    player.play = AsyncMock(return_value=None)

    resp = await cog.handle_music_request(
        "queue this",
        user_id=1,
        guild_id=10,
        tool_args={
            "action": "queue_spotify_playlist",
            "url": "spotify:playlist:abc123",
        },
    )

    assert resp.startswith("[SILENT]")
    assert "Queued 2" in resp
    assert "1 not found" in resp.lower()
    assert player.play.await_count == 2


# ---------------------------------------------------------------------------
# Synced lyrics (lyrics action)
# ---------------------------------------------------------------------------


def _make_cog_with_lyrics(
    lyrics_return=None,
    configured: bool = True,
) -> tuple[MusicCog, MagicMock, MagicMock]:
    cog, player = _make_cog_and_player()

    resolver = MagicMock()
    resolver.fetch = AsyncMock(return_value=lyrics_return)
    cog._lyrics_resolver = resolver if configured else None  # type: ignore[attr-defined]
    return cog, player, resolver


@pytest.mark.asyncio
async def test_lyrics_no_current_track_returns_silent_error() -> None:
    cog, player, resolver = _make_cog_with_lyrics()
    player.current_track = None

    resp = await cog.handle_music_request(
        "show lyrics",
        user_id=1,
        guild_id=10,
        tool_args={"action": "lyrics"},
    )

    assert resp.startswith("[SILENT]")
    assert "nothing" in resp.lower() or "playing" in resp.lower()
    resolver.fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_lyrics_resolver_not_configured_returns_soft_error() -> None:
    cog, player, resolver = _make_cog_with_lyrics(configured=False)
    player.current_track = _t("Some Song")

    resp = await cog.handle_music_request(
        "lyrics",
        user_id=1,
        guild_id=10,
        tool_args={"action": "lyrics"},
    )

    assert resp.startswith("[SILENT]")
    assert "lyrics" in resp.lower()


@pytest.mark.asyncio
async def test_lyrics_synced_found_replies_with_body() -> None:
    from poob.music.lyrics import ParsedLyrics

    lyrics = ParsedLyrics(
        title="Song",
        artist="Artist",
        is_synced=True,
        lines=[(0.0, "Line A"), (10.0, "Line B"), (20.0, "Line C")],
    )
    cog, player, resolver = _make_cog_with_lyrics(lyrics_return=lyrics)
    player.current_track = _t("Song - Artist")

    resp = await cog.handle_music_request(
        "show me the lyrics",
        user_id=1,
        guild_id=10,
        tool_args={"action": "lyrics"},
    )

    assert resp.startswith("[SILENT]")
    assert "Lyrics" in resp
    assert "Line A" in resp
    assert "Line B" in resp
    assert "(plain" not in resp  # synced — no plain-text qualifier
    resolver.fetch.assert_awaited_once()


@pytest.mark.asyncio
async def test_lyrics_only_plain_replies_with_plain_note() -> None:
    from poob.music.lyrics import ParsedLyrics

    lyrics = ParsedLyrics(
        title="Song",
        artist="Artist",
        is_synced=False,
        lines=[(0.0, "Big block of plain text lyrics here.")],
    )
    cog, player, resolver = _make_cog_with_lyrics(lyrics_return=lyrics)
    player.current_track = _t("Some Song")

    resp = await cog.handle_music_request(
        "lyrics",
        user_id=1,
        guild_id=10,
        tool_args={"action": "lyrics"},
    )

    assert resp.startswith("[SILENT]")
    assert "plain" in resp.lower()
    assert "Big block of plain text lyrics" in resp


@pytest.mark.asyncio
async def test_lyrics_resolver_returns_none_means_not_found() -> None:
    cog, player, resolver = _make_cog_with_lyrics(lyrics_return=None)
    player.current_track = _t("Obscure Track")

    resp = await cog.handle_music_request(
        "lyrics",
        user_id=1,
        guild_id=10,
        tool_args={"action": "lyrics"},
    )

    assert resp.startswith("[SILENT]")
    assert "no lyrics" in resp.lower()


@pytest.mark.asyncio
async def test_queue_spotify_playlist_unparseable_url_returns_silent_error() -> None:
    cog, player, resolver = _make_cog_with_spotify(
        is_configured=True,
        resolve_return=None,  # resolver.resolve returns None
    )

    resp = await cog.handle_music_request(
        "queue",
        user_id=1,
        guild_id=10,
        tool_args={
            "action": "queue_spotify_playlist",
            "url": "https://youtube.com/watch?v=foo",
        },
    )

    assert resp.startswith("[SILENT]")
    assert "spotify" in resp.lower()
