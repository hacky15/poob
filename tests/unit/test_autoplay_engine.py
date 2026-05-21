"""Tests for the autoplay cascade engine.

The engine consults three tiers in order — ytmusicapi.get_watch_playlist,
yt-dlp on a YouTube RD<id> mix URL, then a last-resort shuffle of the
caller-supplied history list. Each tier failure is logged but never
propagates: the engine always returns ``Track | None``.

Mocking strategy: ytmusicapi's ``YTMusic`` is patched at the module level
to a MagicMock so tests don't hit the network. ``AsyncYTDL`` is a
MagicMock with the methods the engine consumes stubbed via AsyncMock.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from poob.music.queue import Track, TrackSource


def _track(title: str, video_id: str) -> Track:
    return Track(
        title=title,
        url=f"https://youtu.be/{video_id}",
        duration=timedelta(seconds=180),
        identifier=video_id,
        source=TrackSource.YOUTUBE,
    )


def _engine(
    *,
    ytmusic_result: list[dict] | Exception | None = None,
    ytdl_search_results: dict[str, Track | None] | None = None,
    ytdl_playlist_result: tuple[str, list[dict]] | Exception | None = None,
    history: list[Track] | None = None,
):
    """Build an AutoplayEngine with the tier stubs requested per-test.

    - ytmusic_result: list of dicts (each {"videoId": ..., "title": ...})
        returned from get_watch_playlist's "tracks" key; OR an Exception
        to raise; OR None for "no result".
    - ytdl_search_results: maps query (URL or title) to the Track to
        return. Default returns None for unknown queries.
    - ytdl_playlist_result: tuple from extract_playlist; OR Exception to
        raise; OR None for "no result".
    - history: the queue.history list the engine sees.
    """
    from poob.music.autoplay import AutoplayEngine

    # Stub AsyncYTDL: search + extract_playlist.
    ytdl = MagicMock()

    async def _search(query: str, **_kwargs: Any) -> Track | None:
        if ytdl_search_results is None:
            return None
        return ytdl_search_results.get(query)

    async def _extract_playlist(url: str, **_kwargs: Any) -> tuple[str, list[dict]]:
        if isinstance(ytdl_playlist_result, Exception):
            raise ytdl_playlist_result
        if ytdl_playlist_result is None:
            return ("Unknown Playlist", [])
        return ytdl_playlist_result

    ytdl.search = _search
    ytdl.extract_playlist = _extract_playlist

    # Stub YTMusic class so its instance method get_watch_playlist
    # returns ytmusic_result (or raises). The engine calls
    # YTMusic().get_watch_playlist(videoId=..., limit=...).
    ytmusic_instance = MagicMock()
    if isinstance(ytmusic_result, Exception):
        ytmusic_instance.get_watch_playlist.side_effect = ytmusic_result
    elif ytmusic_result is None:
        ytmusic_instance.get_watch_playlist.return_value = {"tracks": []}
    else:
        ytmusic_instance.get_watch_playlist.return_value = {"tracks": ytmusic_result}

    history_accessor = MagicMock(return_value=history or [])

    engine = AutoplayEngine(
        ytdl=ytdl,
        history_accessor=history_accessor,
        ytmusic_factory=lambda: ytmusic_instance,
    )
    return engine, ytdl, ytmusic_instance, history_accessor


# ---------------------------------------------------------------------------
# 1. No seed -> None (no tier should be consulted)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_seed_returns_none() -> None:
    engine, ytdl, ytmusic, history = _engine()
    result = await engine.get_next(None)
    assert result is None
    # No tier consulted.
    ytmusic.get_watch_playlist.assert_not_called()


# ---------------------------------------------------------------------------
# 2. ytmusic tier returns a track
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ytmusic_tier_returns_track() -> None:
    seed = _track("Bohemian Rhapsody", "fJ9rUzIMcZQ")
    next_track = _track("Don't Stop Believin'", "1k8craCGpgs")
    engine, *_ = _engine(
        ytmusic_result=[
            {"videoId": "1k8craCGpgs", "title": "Don't Stop Believin'"},
        ],
        ytdl_search_results={
            "https://youtu.be/1k8craCGpgs": next_track,
        },
    )
    result = await engine.get_next(seed)
    assert result is next_track


# ---------------------------------------------------------------------------
# 3. ytmusic tier skips the seed itself
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ytmusic_tier_skips_seed() -> None:
    seed = _track("Bohemian Rhapsody", "fJ9rUzIMcZQ")
    next_track = _track("Don't Stop Believin'", "1k8craCGpgs")
    engine, *_ = _engine(
        ytmusic_result=[
            # First candidate IS the seed — must be skipped.
            {"videoId": "fJ9rUzIMcZQ", "title": "Bohemian Rhapsody"},
            {"videoId": "1k8craCGpgs", "title": "Don't Stop Believin'"},
        ],
        ytdl_search_results={
            "https://youtu.be/1k8craCGpgs": next_track,
        },
    )
    result = await engine.get_next(seed)
    assert result is next_track
    assert result.identifier != seed.identifier


# ---------------------------------------------------------------------------
# 4. ytmusic failure falls through to yt-dlp mix URL tier
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ytmusic_failure_falls_to_ytdl_mix() -> None:
    seed = _track("Bohemian Rhapsody", "fJ9rUzIMcZQ")
    next_track = _track("Hotel California", "BciS5krYL80")
    engine, *_ = _engine(
        ytmusic_result=RuntimeError("ytmusicapi blew up"),
        ytdl_playlist_result=(
            "Mix - Bohemian Rhapsody",
            [{"id": "BciS5krYL80", "title": "Hotel California"}],
        ),
        ytdl_search_results={
            "https://youtu.be/BciS5krYL80": next_track,
        },
    )
    result = await engine.get_next(seed)
    assert result is next_track


# ---------------------------------------------------------------------------
# 5. ytdl mix tier skips recently-played entries
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ytdl_mix_skips_recently_played() -> None:
    seed = _track("Bohemian Rhapsody", "fJ9rUzIMcZQ")
    recent = _track("Stairway to Heaven", "QkF3oxziUI4")
    fresh = _track("Hotel California", "BciS5krYL80")
    engine, *_ = _engine(
        ytmusic_result=RuntimeError("force fallback"),
        ytdl_playlist_result=(
            "Mix",
            [
                {"id": "fJ9rUzIMcZQ", "title": "Bohemian Rhapsody"},  # = seed, skip
                {"id": "QkF3oxziUI4", "title": "Stairway"},  # in history, skip
                {"id": "BciS5krYL80", "title": "Hotel California"},  # fresh — take
            ],
        ),
        ytdl_search_results={
            "https://youtu.be/BciS5krYL80": fresh,
        },
        history=[recent],
    )
    result = await engine.get_next(seed)
    assert result is fresh


# ---------------------------------------------------------------------------
# 6. Both YouTube tiers fail -> last-resort history shuffle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_both_tiers_fail_falls_to_history_shuffle() -> None:
    seed = _track("Bohemian Rhapsody", "fJ9rUzIMcZQ")
    historical = _track("Yesterday", "NrgmdOz227I")
    engine, *_ = _engine(
        ytmusic_result=RuntimeError("ytmusicapi down"),
        ytdl_playlist_result=RuntimeError("yt-dlp down"),
        history=[historical, seed],  # seed must be excluded from shuffle
    )
    result = await engine.get_next(seed)
    assert result is historical


# ---------------------------------------------------------------------------
# 7. All tiers fail and history empty -> None
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_all_tiers_fail_returns_none() -> None:
    seed = _track("Bohemian Rhapsody", "fJ9rUzIMcZQ")
    engine, *_ = _engine(
        ytmusic_result=RuntimeError("down"),
        ytdl_playlist_result=RuntimeError("down"),
        history=[],
    )
    result = await engine.get_next(seed)
    assert result is None


# ---------------------------------------------------------------------------
# 8. The engine swallows exceptions from any tier — never raises
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_engine_swallows_all_exceptions() -> None:
    seed = _track("Bohemian Rhapsody", "fJ9rUzIMcZQ")
    engine, *_ = _engine(
        ytmusic_result=RuntimeError("boom"),
        ytdl_playlist_result=RuntimeError("kaboom"),
        history=[],
    )
    # No raise; result is None when nothing recovers.
    result = await engine.get_next(seed)
    assert result is None


# ---------------------------------------------------------------------------
# 9. Seed without a YouTube identifier still tries history shuffle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_seed_without_identifier_falls_to_history() -> None:
    seed = Track(
        title="Some Direct URL Track",
        url="https://example.com/track.mp3",
        identifier=None,
        source=TrackSource.DIRECT_URL,
    )
    historical = _track("Yesterday", "NrgmdOz227I")
    engine, ytdl, ytmusic, _ = _engine(
        history=[historical],
    )
    result = await engine.get_next(seed)
    assert result is historical
    # ytmusic tier shouldn't have been consulted — no identifier to seed with.
    ytmusic.get_watch_playlist.assert_not_called()
