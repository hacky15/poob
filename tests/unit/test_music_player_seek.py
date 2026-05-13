"""Tests for ``GuildMusicPlayer.seek`` — absolute + relative + clamping.

The position-tracker and respawn machinery live in
``test_music_player_primitives.py``; this file covers the seek-specific
behavior: clamp at 0, clamp before duration, no-op for streams, no-op
without a current track, and the relative-vs-absolute branch.
"""

from __future__ import annotations

import time
from datetime import timedelta
from unittest.mock import MagicMock

import pytest

from poob.music.player import GuildMusicPlayer
from poob.music.queue import Track
from poob.music.seek import ParsedSeek


def _t(name: str, *, duration_s: int = 180, is_stream: bool = False) -> Track:
    return Track(
        title=name, url=f"https://example.com/{name}",
        duration=None if is_stream else timedelta(seconds=duration_s),
        is_stream=is_stream,
    )


def _make_player() -> GuildMusicPlayer:
    vc = MagicMock()
    vc.is_playing.return_value = True
    vc.is_connected.return_value = True
    return GuildMusicPlayer(voice_client=vc, ytdl=MagicMock(), volume=0.5, idle_timeout=0)


# ---------------------------------------------------------------------------
# No current track / stream — early-return contracts
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_seek_returns_none_when_no_current_track() -> None:
    p = _make_player()
    assert p.queue.current is None

    result = await p.seek(ParsedSeek(seconds=30.0, relative=False))

    assert result is None
    assert p._respawn_request is None


@pytest.mark.asyncio
async def test_seek_returns_none_on_livestream() -> None:
    """Seeking a livestream is nonsensical — return None, let the
    handler surface a user-readable error."""
    p = _make_player()
    p.queue.current = _t("LiveStream", is_stream=True)
    p._track_started_at = time.monotonic() - 5.0

    result = await p.seek(ParsedSeek(seconds=30.0, relative=False))

    assert result is None
    assert p._respawn_request is None


# ---------------------------------------------------------------------------
# Absolute target
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_seek_absolute_sets_target_directly() -> None:
    p = _make_player()
    track = _t("song", duration_s=180)
    p.queue.current = track
    p._track_started_at = time.monotonic() - 10.0

    result = await p.seek(ParsedSeek(seconds=45.0, relative=False))

    assert result is not None
    returned_track, target = result
    assert returned_track is track
    assert target == 45.0
    assert p._respawn_request is not None
    req_track, req_pos, _ = p._respawn_request
    assert req_track is track
    assert req_pos == 45.0


@pytest.mark.asyncio
async def test_seek_absolute_clamps_negative_to_zero() -> None:
    """Negative absolute shouldn't actually happen (parser rejects
    negative clocks; negative bare seconds parse as relative) but the
    player clamp is the defense-in-depth."""
    p = _make_player()
    p.queue.current = _t("song", duration_s=180)
    p._track_started_at = time.monotonic() - 10.0

    _, target = await p.seek(ParsedSeek(seconds=-10.0, relative=False))  # type: ignore[misc]

    assert target == 0.0


@pytest.mark.asyncio
async def test_seek_absolute_clamps_to_duration_minus_one() -> None:
    """Seeking past the track end is treated as "1s before end", not as
    skip. Skipping is a separate verb."""
    p = _make_player()
    p.queue.current = _t("song", duration_s=180)
    p._track_started_at = time.monotonic() - 10.0

    _, target = await p.seek(ParsedSeek(seconds=600.0, relative=False))  # type: ignore[misc]

    assert target == 179.0  # 180 - 1


# ---------------------------------------------------------------------------
# Relative target
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_seek_relative_adds_to_current_position() -> None:
    p = _make_player()
    p.queue.current = _t("song", duration_s=180)
    p._track_started_at = time.monotonic() - 30.0  # ~30s in
    p._total_pause_seconds = 0.0

    result = await p.seek(ParsedSeek(seconds=10.0, relative=True))

    assert result is not None
    _, target = result
    # Position was ~30, +10 → ~40 (with a slight ±0.1s wall-clock margin)
    assert 39.9 < target < 40.1


@pytest.mark.asyncio
async def test_seek_relative_negative_clamps_at_zero() -> None:
    p = _make_player()
    p.queue.current = _t("song", duration_s=180)
    p._track_started_at = time.monotonic() - 5.0
    p._total_pause_seconds = 0.0

    result = await p.seek(ParsedSeek(seconds=-30.0, relative=True))

    _, target = result  # type: ignore[misc]
    assert target == 0.0


@pytest.mark.asyncio
async def test_seek_relative_clamps_when_target_exceeds_duration() -> None:
    p = _make_player()
    p.queue.current = _t("song", duration_s=180)
    p._track_started_at = time.monotonic() - 170.0
    p._total_pause_seconds = 0.0

    _, target = await p.seek(ParsedSeek(seconds=30.0, relative=True))  # type: ignore[misc]

    # 170 + 30 = 200 → clamped to 179
    assert target == 179.0


# ---------------------------------------------------------------------------
# Effect preservation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_seek_preserves_active_effect_chain() -> None:
    """Seeking under nightcore stays nightcored. Same contract as replay."""
    p = _make_player()
    p.queue.current = _t("song", duration_s=180)
    p._track_started_at = time.monotonic() - 10.0
    p._active_effect = "nightcore"
    p._active_effect_chain = "asetrate=44100*1.25,aresample=44100"

    await p.seek(ParsedSeek(seconds=45.0, relative=False))

    _, _, chain = p._respawn_request  # type: ignore[misc]
    assert chain == "asetrate=44100*1.25,aresample=44100"


# ---------------------------------------------------------------------------
# Track without duration metadata
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_seek_with_unknown_duration_skips_upper_clamp() -> None:
    """yt-dlp occasionally returns no duration on non-stream tracks
    (e.g. some podcast extractions). Don't clamp upward; let FFmpeg
    end-of-stream naturally if user seeks past."""
    p = _make_player()
    no_dur = Track(title="podcast", url="x", duration=None, is_stream=False)
    p.queue.current = no_dur
    p._track_started_at = time.monotonic() - 5.0

    _, target = await p.seek(ParsedSeek(seconds=9999.0, relative=False))  # type: ignore[misc]

    # No upper clamp without duration; just the lower 0-clamp.
    assert target == 9999.0
