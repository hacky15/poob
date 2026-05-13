"""Tests for player-level music primitives: replay, previous, set_effect.

These exercise state mutations on GuildMusicPlayer in isolation
(``voice_client`` is a Mock; we don't run the full ``_player_loop``).
The integration story — that the respawn flag is honored by the loop —
is smoke-tested in prod. The unit tests here lock down the contracts
each method exposes to the music handler:

- ``replay()`` requests a respawn of current at position 0
- ``previous()`` swaps current<->history-pop and requests respawn
- ``set_effect()`` captures current position and requests respawn
- ``position_seconds`` is wall-clock since play minus paused intervals
- ``previous()`` with empty history is a no-op returning None
- ``replay()`` / ``set_effect()`` with no current track are no-ops

See docs/decisions/music-queue-primitives.md and
docs/decisions/music-on-the-fly-filter-respawn.md for the design.
"""

from __future__ import annotations

import time
from datetime import timedelta
from unittest.mock import MagicMock

import pytest

from poob.music.player import GuildMusicPlayer
from poob.music.queue import Track


def _t(name: str) -> Track:
    return Track(
        title=name, url=f"https://example.com/{name}",
        duration=timedelta(seconds=180),
    )


def _make_player() -> GuildMusicPlayer:
    """Player with fake VC + fake YTDL — enough state to mutate queue."""
    vc = MagicMock()
    vc.is_playing.return_value = True
    vc.is_connected.return_value = True
    ytdl = MagicMock()
    return GuildMusicPlayer(voice_client=vc, ytdl=ytdl, volume=0.5, idle_timeout=0)


# ---------------------------------------------------------------------------
# replay()
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_replay_with_no_current_returns_none() -> None:
    p = _make_player()
    assert p.queue.current is None

    result = await p.replay()

    assert result is None
    assert p._respawn_request is None


@pytest.mark.asyncio
async def test_replay_requests_respawn_of_current_at_position_zero() -> None:
    p = _make_player()
    cur = _t("currently-playing")
    p.queue.current = cur

    result = await p.replay()

    assert result is cur
    assert p._respawn_request is not None
    track, position, _effect = p._respawn_request
    assert track is cur
    assert position == 0.0
    p.voice_client.stop.assert_called_once()


@pytest.mark.asyncio
async def test_replay_preserves_active_effect_chain() -> None:
    """Replay restarts at 0 — should keep the user's active effect on
    so 'replay' under nightcore stays nightcored."""
    p = _make_player()
    cur = _t("song")
    p.queue.current = cur
    p._active_effect = "nightcore"
    p._active_effect_chain = "asetrate=44100*1.25,aresample=44100"

    await p.replay()

    _track, _pos, chain = p._respawn_request  # type: ignore[misc]
    assert chain == "asetrate=44100*1.25,aresample=44100"


# ---------------------------------------------------------------------------
# previous()
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_previous_with_empty_history_returns_none() -> None:
    p = _make_player()
    cur = _t("current")
    p.queue.current = cur

    result = await p.previous()

    assert result is None
    assert p._respawn_request is None
    assert p.queue.current is cur


@pytest.mark.asyncio
async def test_previous_swaps_current_with_history_pop_and_inserts_old_to_queue() -> None:
    p = _make_player()
    a, b, c = _t("A"), _t("B"), _t("C")
    p.queue.add(a); p.queue.add(b); p.queue.add(c)
    p.queue.get_next()  # current=A
    p.queue.get_next()  # current=B, history=[A]

    result = await p.previous()

    assert result is a
    assert p.queue.current is a
    # Old current (B) should be at the FRONT of the queue so it plays
    # next after A finishes.
    assert p.queue.upcoming[0] is b
    # History should be empty now (A was popped).
    assert p.queue.history == []
    # Respawn requested at position 0 with no effect (default).
    assert p._respawn_request is not None
    track, position, _ = p._respawn_request
    assert track is a
    assert position == 0.0


@pytest.mark.asyncio
async def test_previous_with_no_current_still_walks_history() -> None:
    """If somehow current is None but history has entries, walk back to
    the most recent. Defensive — the loop sets current=None between
    tracks. Without this case, `previous` would lock up after a
    natural track-end while waiting for the next track."""
    p = _make_player()
    a = _t("A")
    p.queue._history.append(a)
    assert p.queue.current is None

    result = await p.previous()

    assert result is a
    assert p.queue.current is a


# ---------------------------------------------------------------------------
# set_effect()
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_set_effect_with_no_current_returns_none() -> None:
    p = _make_player()
    result = await p.set_effect("nightcore")
    assert result is None
    assert p._respawn_request is None
    # But the effect should be stored as the default for the next track.
    assert p._active_effect == "nightcore"


@pytest.mark.asyncio
async def test_set_effect_requests_respawn_at_current_position() -> None:
    p = _make_player()
    cur = _t("song")
    p.queue.current = cur
    # Simulate having played for ~5 seconds.
    p._track_started_at = time.monotonic() - 5.0
    p._total_pause_seconds = 0.0

    result = await p.set_effect("nightcore")

    assert result == "nightcore"
    assert p._respawn_request is not None
    track, position, chain = p._respawn_request
    assert track is cur
    assert 4.5 < position < 5.5
    assert "asetrate=44100*1.25" in chain
    p.voice_client.stop.assert_called_once()


@pytest.mark.asyncio
async def test_set_effect_none_clears_filter_chain() -> None:
    p = _make_player()
    cur = _t("song")
    p.queue.current = cur
    p._track_started_at = time.monotonic() - 2.0
    p._active_effect = "nightcore"
    p._active_effect_chain = "asetrate=44100*1.25,aresample=44100"

    result = await p.set_effect("none")

    assert result == "none"
    assert p._active_effect == "none"
    assert p._active_effect_chain is None
    track, _pos, chain = p._respawn_request  # type: ignore[misc]
    assert chain is None


@pytest.mark.asyncio
async def test_set_effect_invalid_name_raises_and_does_not_mutate() -> None:
    from poob.music.effects import EffectNotFoundError

    p = _make_player()
    cur = _t("song")
    p.queue.current = cur
    p._track_started_at = time.monotonic() - 2.0

    with pytest.raises(EffectNotFoundError):
        await p.set_effect("notarealeffect")

    assert p._respawn_request is None
    assert p._active_effect == "none"


# ---------------------------------------------------------------------------
# Position tracker
# ---------------------------------------------------------------------------

def test_position_seconds_zero_when_no_track_playing() -> None:
    p = _make_player()
    assert p.position_seconds == 0.0


def test_position_seconds_is_wall_clock_since_play() -> None:
    p = _make_player()
    p._track_started_at = time.monotonic() - 3.5
    p._total_pause_seconds = 0.0

    pos = p.position_seconds

    assert 3.4 < pos < 3.6


def test_position_seconds_excludes_paused_duration() -> None:
    """While paused, the tracker freezes — position must not advance.
    Total pause seconds gets subtracted from wall-clock delta."""
    p = _make_player()
    p._track_started_at = time.monotonic() - 10.0
    p._total_pause_seconds = 3.0

    pos = p.position_seconds

    assert 6.9 < pos < 7.1


def test_position_seconds_excludes_in_flight_pause() -> None:
    """If currently paused, the in-flight pause interval must also be
    excluded — otherwise the user pausing for 5 minutes then resuming
    would seek 5 minutes into the track."""
    p = _make_player()
    p._track_started_at = time.monotonic() - 10.0
    p._total_pause_seconds = 0.0
    p._paused_at = time.monotonic() - 4.0  # paused 4s ago

    pos = p.position_seconds

    # 10s elapsed, but the last 4s was paused → ~6s real position
    assert 5.9 < pos < 6.1
