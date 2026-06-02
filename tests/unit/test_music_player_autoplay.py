"""Tests for the player's queue-empty autoplay hook.

These tests don't spin a real GuildMusicPlayer (it needs a VoiceClient
and audio threads). They cover the small testable surface:
the ``_try_autoplay_inject`` helper, which sits between the queue-empty
branch and the existing "break" path. The helper holds all the
autoplay-specific decision logic; the surrounding player loop only
needs to know "this returned a Track? Use it. None? Break."

The mock strategy isolates ``_try_autoplay_inject`` by building a
minimal player-shaped object that has the attributes the helper reads
and consumes a mock AutoplayEngine.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from poob.music.queue import MusicQueue, Track, TrackSource


def _track(title: str, video_id: str) -> Track:
    return Track(
        title=title,
        url=f"https://youtu.be/{video_id}",
        duration=timedelta(seconds=180),
        identifier=video_id,
        source=TrackSource.YOUTUBE,
    )


def _make_player_for_autoplay(
    *,
    autoplay_enabled: bool,
    last_played: Track | None,
    engine_returns: Track | None,
    engine_raises: Exception | None = None,
):
    """Build a duck-typed player exposing only what ``_try_autoplay_inject`` needs.

    Returns ``(player, engine_mock)``.
    """
    from poob.music.player import GuildMusicPlayer

    # We bind the method off the class so we can call it on a duck.
    queue = MusicQueue()

    engine = MagicMock()
    if engine_raises is not None:
        engine.get_next = AsyncMock(side_effect=engine_raises)
    else:
        engine.get_next = AsyncMock(return_value=engine_returns)

    player_stub = MagicMock()
    player_stub.autoplay_enabled = autoplay_enabled
    player_stub._last_played_track = last_played
    player_stub.queue = queue
    player_stub._get_autoplay_engine = MagicMock(return_value=engine)

    # Bind the real method to the stub so it runs with stub attributes.
    player_stub._try_autoplay_inject = GuildMusicPlayer._try_autoplay_inject.__get__(
        player_stub, type(player_stub),
    )

    return player_stub, engine


# ---------------------------------------------------------------------------
# 1. autoplay disabled -> helper returns None without consulting engine
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_queue_empty_with_autoplay_off_returns_none() -> None:
    player, engine = _make_player_for_autoplay(
        autoplay_enabled=False,
        last_played=_track("Seed", "abc"),
        engine_returns=_track("Next", "def"),  # ignored
    )
    result = await player._try_autoplay_inject()
    assert result is None
    engine.get_next.assert_not_called()


# ---------------------------------------------------------------------------
# 2. autoplay on + engine produces a track -> enqueued and returned
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_queue_empty_with_autoplay_on_enqueues_and_returns_track() -> None:
    seed = _track("Seed", "abc")
    generated = _track("Generated", "def")
    player, engine = _make_player_for_autoplay(
        autoplay_enabled=True,
        last_played=seed,
        engine_returns=generated,
    )
    result = await player._try_autoplay_inject()
    assert result is generated
    engine.get_next.assert_awaited_once_with(seed)
    # The track must also become queue.current (popped via get_next).
    assert player.queue.current is generated


# ---------------------------------------------------------------------------
# 3. autoplay on + engine returns None -> helper returns None (loop will break)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_queue_empty_with_autoplay_on_but_engine_returns_none() -> None:
    player, engine = _make_player_for_autoplay(
        autoplay_enabled=True,
        last_played=_track("Seed", "abc"),
        engine_returns=None,
    )
    result = await player._try_autoplay_inject()
    assert result is None
    engine.get_next.assert_awaited_once()


# ---------------------------------------------------------------------------
# 4. autoplay on + no last-played seed -> skip the engine, return None
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_queue_empty_with_no_last_played_skips_engine() -> None:
    player, engine = _make_player_for_autoplay(
        autoplay_enabled=True,
        last_played=None,
        engine_returns=_track("Generated", "def"),  # ignored
    )
    result = await player._try_autoplay_inject()
    assert result is None
    engine.get_next.assert_not_called()


# ---------------------------------------------------------------------------
# 5. engine raising never leaks — helper returns None
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_engine_exception_returns_none_no_propagation() -> None:
    player, engine = _make_player_for_autoplay(
        autoplay_enabled=True,
        last_played=_track("Seed", "abc"),
        engine_returns=None,
        engine_raises=RuntimeError("engine blew up"),
    )
    # MUST NOT raise.
    result = await player._try_autoplay_inject()
    assert result is None


# ---------------------------------------------------------------------------
# 6. Set/get the autoplay flag at the player level
# ---------------------------------------------------------------------------


def test_default_autoplay_state_is_off() -> None:
    """Newly constructed players start with autoplay disabled."""
    from poob.music.player import GuildMusicPlayer

    vc = MagicMock()
    ytdl = MagicMock()
    player = GuildMusicPlayer(voice_client=vc, ytdl=ytdl)
    assert player.autoplay_enabled is False


def test_autoplay_can_be_toggled_on_and_off() -> None:
    from poob.music.player import GuildMusicPlayer

    vc = MagicMock()
    ytdl = MagicMock()
    player = GuildMusicPlayer(voice_client=vc, ytdl=ytdl)
    player.autoplay_enabled = True
    assert player.autoplay_enabled is True
    player.autoplay_enabled = False
    assert player.autoplay_enabled is False


@pytest.mark.asyncio
async def test_stop_disables_autoplay() -> None:
    """``stop`` must turn autoplay OFF. Otherwise stop clears the queue and
    halts the track, but the player loop wakes, sees an empty queue, and
    (autoplay still on) injects a fresh track — so "stop" never actually
    stops and reports a transient "no music" during the refill race.
    See docs/incidents/stop-does-not-disable-autoplay.md.
    """
    from poob.music.player import GuildMusicPlayer

    vc = MagicMock()
    vc.is_playing.return_value = False
    ytdl = MagicMock()
    player = GuildMusicPlayer(voice_client=vc, ytdl=ytdl)
    player.autoplay_enabled = True
    await player.stop()
    assert player.autoplay_enabled is False
