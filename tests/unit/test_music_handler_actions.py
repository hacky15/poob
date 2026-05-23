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
from poob.music.queue import Track


def _t(name: str) -> Track:
    return Track(
        title=name, url=f"https://example.com/{name}",
        duration=timedelta(seconds=180),
    )


def _make_cog_and_player() -> tuple[MusicCog, MagicMock]:
    """Build a MusicCog with a stubbed player + ytdl + guild/member graph."""
    from poob.config import AppConfig

    bot = MagicMock()
    config = AppConfig(
        discord_token="x",
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
        "go back", user_id=1, guild_id=10,
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
        "previous", user_id=1, guild_id=10,
        tool_args={"action": "previous"},
    )

    assert resp.startswith("[SILENT]")
    assert "Old Song" in resp


@pytest.mark.asyncio
async def test_replay_with_no_current_returns_silent_error() -> None:
    cog, player = _make_cog_and_player()
    player.replay = AsyncMock(return_value=None)

    resp = await cog.handle_music_request(
        "replay", user_id=1, guild_id=10,
        tool_args={"action": "replay"},
    )

    assert resp.startswith("[SILENT]")
    assert "nothing" in resp.lower()


@pytest.mark.asyncio
async def test_replay_with_current_returns_titled_silent_reply() -> None:
    cog, player = _make_cog_and_player()
    player.replay = AsyncMock(return_value=_t("Now Playing"))

    resp = await cog.handle_music_request(
        "replay", user_id=1, guild_id=10,
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
        "move", user_id=1, guild_id=10,
        tool_args={"action": "move", "from_position": 1, "to_position": 3},
    )

    player.queue.move.assert_called_once_with(0, 2)
    assert resp.startswith("[SILENT]")
    assert "Track" in resp


@pytest.mark.asyncio
async def test_move_missing_positions_returns_silent_error() -> None:
    cog, player = _make_cog_and_player()

    resp = await cog.handle_music_request(
        "move", user_id=1, guild_id=10,
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
        "move", user_id=1, guild_id=10,
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
        "remove", user_id=1, guild_id=10,
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
        "clear", user_id=1, guild_id=10,
        tool_args={"action": "clear"},
    )

    assert resp.startswith("[SILENT]")
    assert "5" in resp


# ---------------------------------------------------------------------------
# apply_effect
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_apply_effect_dispatches_to_player_set_effect() -> None:
    cog, player = _make_cog_and_player()
    player.set_effect = AsyncMock(return_value="nightcore")
    player.active_effect = "nightcore"

    resp = await cog.handle_music_request(
        "nightcore it", user_id=1, guild_id=10,
        tool_args={"action": "apply_effect", "effect": "nightcore"},
    )

    player.set_effect.assert_awaited_once_with("nightcore")
    assert resp.startswith("[SILENT]")


@pytest.mark.asyncio
async def test_apply_effect_none_returns_cleared_message() -> None:
    cog, player = _make_cog_and_player()
    player.set_effect = AsyncMock(return_value="none")
    player.active_effect = "none"

    resp = await cog.handle_music_request(
        "clear the effect", user_id=1, guild_id=10,
        tool_args={"action": "apply_effect", "effect": "none"},
    )

    assert resp.startswith("[SILENT]")
    assert "clear" in resp.lower()


@pytest.mark.asyncio
async def test_apply_effect_unknown_returns_silent_error_with_valid_names() -> None:
    from poob.music.effects import EffectNotFoundError

    cog, player = _make_cog_and_player()
    player.set_effect = AsyncMock(side_effect=EffectNotFoundError("unknown effect 'wahwah'; valid: none, nightcore"))

    resp = await cog.handle_music_request(
        "wahwah", user_id=1, guild_id=10,
        tool_args={"action": "apply_effect", "effect": "wahwah"},
    )

    assert resp.startswith("[SILENT]")
    assert "wahwah" in resp


@pytest.mark.asyncio
async def test_apply_effect_no_effect_arg_returns_help() -> None:
    cog, _ = _make_cog_and_player()

    resp = await cog.handle_music_request(
        "apply an effect", user_id=1, guild_id=10,
        tool_args={"action": "apply_effect"},
    )

    assert resp.startswith("[SILENT]")
    # Should list the available presets so the user knows the surface.
    assert "nightcore" in resp.lower()
    assert "slowed" in resp.lower()


# ---------------------------------------------------------------------------
# queue_many
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_queue_many_all_resolve_reports_count() -> None:
    cog, player = _make_cog_and_player()
    player.play = AsyncMock()
    cog._ytdl = MagicMock()
    cog._ytdl.search = AsyncMock(side_effect=[
        _t("Bohemian Rhapsody"), _t("Don't Stop Believin'"), _t("Africa"),
    ])
    player.is_playing = False

    resp = await cog.handle_music_request(
        "play three songs", user_id=1, guild_id=10,
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
    cog._ytdl.search = AsyncMock(side_effect=[
        _t("Bohemian Rhapsody"), None, _t("Africa"),
    ])
    player.is_playing = True

    resp = await cog.handle_music_request(
        "queue stuff", user_id=1, guild_id=10,
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
        "queue stuff", user_id=1, guild_id=10,
        tool_args={"action": "queue_many", "tracks": ["x", "y", "z"]},
    )

    player.play.assert_not_called()
    assert "couldn't find" in resp.lower()


@pytest.mark.asyncio
async def test_queue_many_empty_list_returns_silent_error() -> None:
    cog, player = _make_cog_and_player()

    resp = await cog.handle_music_request(
        "queue many", user_id=1, guild_id=10,
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
        "queue many", user_id=1, guild_id=10,
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
        "previous", "replay", "move", "remove", "clear",
        "queue_many", "apply_effect", "seek",
    ):
        assert action in enum, f"MUSIC_TOOL missing {action!r}"


def test_music_tool_schema_declares_new_parameters() -> None:
    from poob.brain.poob import MUSIC_TOOL

    props = MUSIC_TOOL["function"]["parameters"]["properties"]
    for param in (
        "tracks", "from_position", "to_position", "position", "effect", "time",
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
        "seek to two thirty", user_id=1, guild_id=10,
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
        "skip ahead", user_id=1, guild_id=10,
        tool_args={"action": "seek", "time": "+10s"},
    )

    parsed_arg = player.seek.await_args.args[0]
    assert parsed_arg.seconds == 10.0
    assert parsed_arg.relative is True


@pytest.mark.asyncio
async def test_seek_missing_time_arg_returns_silent_prompt() -> None:
    cog, player = _make_cog_and_player()

    resp = await cog.handle_music_request(
        "seek", user_id=1, guild_id=10,
        tool_args={"action": "seek"},
    )

    assert resp.startswith("[SILENT]")
    assert "2:30" in resp or "+10" in resp  # format hint in the prompt


@pytest.mark.asyncio
async def test_seek_invalid_format_returns_silent_parse_error() -> None:
    cog, player = _make_cog_and_player()

    resp = await cog.handle_music_request(
        "seek to the future", user_id=1, guild_id=10,
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
        "seek", user_id=1, guild_id=10,
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
        "leave", user_id=1, guild_id=10,
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
        "leave", user_id=1, guild_id=10,
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
        "seek", user_id=1, guild_id=10,
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
        "autoplay on", user_id=1, guild_id=10,
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
        "autoplay off", user_id=1, guild_id=10,
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
        "autoplay?", user_id=1, guild_id=10,
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
        "autoplay", user_id=1, guild_id=10,
        tool_args={"action": "autoplay"},  # no mode field
    )

    assert resp.startswith("[SILENT]")
    assert "mode" in resp.lower() or "on" in resp.lower()
    assert player.autoplay_enabled is False  # untouched on bad input


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
        "save as chill", user_id=1, guild_id=10,
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
        "save as chill", user_id=1, guild_id=10,
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
        "save", user_id=1, guild_id=10,
        tool_args={"action": "save_playlist", "name": "chill"},
    )

    assert "Updated" in resp
    assert "chill" in resp
    repo.save.assert_awaited_once()


@pytest.mark.asyncio
async def test_save_playlist_missing_name_returns_help() -> None:
    cog, player, repo = _make_cog_with_playlist_repo()

    resp = await cog.handle_music_request(
        "save", user_id=1, guild_id=10,
        tool_args={"action": "save_playlist"},  # no name
    )

    assert resp.startswith("[SILENT]")
    assert "name" in resp.lower()
    repo.save.assert_not_awaited()


@pytest.mark.asyncio
async def test_load_playlist_appends_to_queue() -> None:
    cog, player, repo = _make_cog_with_playlist_repo()
    saved_tracks = [
        {"title": "T1", "url": "u1", "identifier": "i1",
         "duration_seconds": 60, "source": "youtube", "is_stream": False},
        {"title": "T2", "url": "u2", "identifier": "i2",
         "duration_seconds": 90, "source": "youtube", "is_stream": False},
        {"title": "T3", "url": "u3", "identifier": "i3",
         "duration_seconds": 120, "source": "youtube", "is_stream": False},
    ]
    repo.load = AsyncMock(return_value=saved_tracks)

    resp = await cog.handle_music_request(
        "load chill", user_id=1, guild_id=10,
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
        "load chill", user_id=1, guild_id=10,
        tool_args={"action": "load_playlist", "name": "chill"},
    )

    assert resp.startswith("[SILENT]")
    assert "no playlist" in resp.lower()
    player.queue.add.assert_not_called()


@pytest.mark.asyncio
async def test_load_playlist_missing_name_returns_help() -> None:
    cog, player, repo = _make_cog_with_playlist_repo()

    resp = await cog.handle_music_request(
        "load", user_id=1, guild_id=10,
        tool_args={"action": "load_playlist"},
    )

    assert "name" in resp.lower()
    repo.load.assert_not_awaited()


@pytest.mark.asyncio
async def test_list_playlists_returns_alphabetical_names() -> None:
    cog, player, repo = _make_cog_with_playlist_repo()
    repo.list_names = AsyncMock(return_value=["chill", "deep-focus", "gym"])

    resp = await cog.handle_music_request(
        "list my playlists", user_id=1, guild_id=10,
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
        "list", user_id=1, guild_id=10,
        tool_args={"action": "list_playlists"},
    )

    assert resp.startswith("[SILENT]")
    assert "no saved" in resp.lower() or "no playlists" in resp.lower()


@pytest.mark.asyncio
async def test_delete_playlist_removes_and_replies() -> None:
    cog, player, repo = _make_cog_with_playlist_repo()
    repo.delete = AsyncMock(return_value=True)

    resp = await cog.handle_music_request(
        "forget chill", user_id=1, guild_id=10,
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
        "forget nonexistent", user_id=1, guild_id=10,
        tool_args={"action": "delete_playlist", "name": "nonexistent"},
    )

    assert resp.startswith("[SILENT]")
    assert "no playlist" in resp.lower()


@pytest.mark.asyncio
async def test_delete_playlist_missing_name_returns_help() -> None:
    cog, player, repo = _make_cog_with_playlist_repo()

    resp = await cog.handle_music_request(
        "delete", user_id=1, guild_id=10,
        tool_args={"action": "delete_playlist"},
    )

    assert "name" in resp.lower()
    repo.delete.assert_not_awaited()
