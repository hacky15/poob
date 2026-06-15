"""Tests for the dedup'd track resolver — no double-downloads anywhere.

Root cause (audit 2026-06-15): ``play(deferred=True)`` fired ``_pre_resolve``
as a fire-and-forget task with no shared handle, and the player loop's
acquisition re-checked only ``track.local_file``/``stream_url`` with no
in-flight guard. When ``start_deferred()`` launched the loop before the
pre-resolve download finished, the track was downloaded TWICE to two temp
files — one played, one orphaned on disk (violating the music-player
"temp files must be cleaned up" invariant + adding ~1-2s start latency).

Fix: a single dedup'd resolver. ``_ensure_resolving(track)`` returns one
in-flight task per Track instance; deferred-play, prefetch, and the
player-loop acquisition all share it, so a track is never downloaded twice.

See docs/incidents/deferred-double-download.md.
"""

from __future__ import annotations

import asyncio
import inspect
from datetime import timedelta

import pytest

from poob.music.player import GuildMusicPlayer
from poob.music.queue import MusicQueue, Track


class FakeYTDL:
    """Mimics AsyncYTDL: download_track is idempotent on track.local_file
    (returns the cached path without re-downloading), exactly like the real
    one at ytdl.py:367."""

    def __init__(self) -> None:
        self.download_calls = 0
        self.stream_calls = 0
        self.fail_download = False

    async def download_track(self, track: Track, *, timeout: float = 30.0) -> str | None:
        if track.local_file:  # idempotent cache check (real ytdl.py:367-368)
            return track.local_file
        self.download_calls += 1
        await asyncio.sleep(0.01)  # real work — makes the race observable
        if self.fail_download:
            return None
        track.local_file = f"/tmp/{track.title}.webm"
        return track.local_file

    async def resolve_stream_url(self, track: Track) -> str | None:
        self.stream_calls += 1
        track.stream_url = f"http://stream/{track.title}"
        return track.stream_url


def _player(ytdl: FakeYTDL) -> GuildMusicPlayer:
    p = GuildMusicPlayer.__new__(GuildMusicPlayer)
    p._loop = asyncio.get_event_loop()
    p.ytdl = ytdl  # type: ignore[assignment]
    p._resolve_tasks = {}
    p.queue = MusicQueue()
    p._player_task = None
    return p


def _t(name: str = "A") -> Track:
    return Track(title=name, url=f"https://example.com/{name}", duration=timedelta(seconds=180))


@pytest.mark.asyncio
async def test_ensure_resolving_dedupes_concurrent_resolves() -> None:
    """Two resolves of the same in-flight track share ONE task → one download."""
    ytdl = FakeYTDL()
    p = _player(ytdl)
    t = _t()

    task1 = p._ensure_resolving(t)
    task2 = p._ensure_resolving(t)

    assert task1 is task2, "concurrent resolves must share one in-flight task"
    await task1
    assert ytdl.download_calls == 1
    assert t.local_file is not None


@pytest.mark.asyncio
async def test_deferred_race_downloads_once() -> None:
    """The exact prod race: play(deferred=True) registers a resolve, then the
    player-loop acquisition awaits the SAME task instead of racing a second
    download. This is what produced the orphaned temp file."""
    ytdl = FakeYTDL()
    p = _player(ytdl)
    t = _t()

    await p.play(t, deferred=True)        # registers the resolve task
    await p._ensure_resolving(t)          # what the loop does to acquire audio

    assert ytdl.download_calls == 1, "deferred play + loop acquisition double-downloaded"


@pytest.mark.asyncio
async def test_resolve_falls_back_to_stream_url() -> None:
    """If download fails, the resolver falls back to a stream URL (the loop's
    old behavior is preserved)."""
    ytdl = FakeYTDL()
    ytdl.fail_download = True
    p = _player(ytdl)
    t = _t()

    await p._ensure_resolving(t)

    assert ytdl.download_calls == 1
    assert ytdl.stream_calls == 1
    assert t.stream_url is not None


@pytest.mark.asyncio
async def test_resolve_task_cleared_when_done() -> None:
    """Finished resolves drop out of the in-flight map (no unbounded growth)."""
    ytdl = FakeYTDL()
    p = _player(ytdl)
    t = _t()

    await p._ensure_resolving(t)
    await asyncio.sleep(0)  # let the done-callback run

    assert p._resolve_tasks == {}


@pytest.mark.asyncio
async def test_already_resolved_track_does_not_download() -> None:
    """A track that already has local_file set is never re-downloaded."""
    ytdl = FakeYTDL()
    p = _player(ytdl)
    t = _t()
    t.local_file = "/tmp/cached.webm"

    await p._ensure_resolving(t)

    assert ytdl.download_calls == 0


def test_pre_resolve_is_gone_and_loop_uses_shared_resolver() -> None:
    """The fragile fire-and-forget _pre_resolve must be removed (no zombie
    name), and the player loop must acquire audio via the shared resolver."""
    src = inspect.getsource(GuildMusicPlayer)
    assert "_pre_resolve" not in src, (
        "_pre_resolve (fire-and-forget, no shared handle) must be removed — "
        "it is the double-download root cause. See "
        "docs/incidents/deferred-double-download.md."
    )
    loop_src = inspect.getsource(GuildMusicPlayer._player_loop)
    assert "_ensure_resolving" in loop_src, (
        "player loop must acquire audio via the dedup'd _ensure_resolving, "
        "not a bare download_track that races the deferred pre-resolve."
    )
