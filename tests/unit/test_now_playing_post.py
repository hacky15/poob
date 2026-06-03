"""Tests for the now-playing card post-when-ready helper.

A freshly-played track must download before the player sets it as `current`,
so the card can't be posted synchronously right after the brain reply (the
old race: fresh plays showed no card, skips to pre-fetched tracks did). The
helper waits — bounded + non-blocking — for `current_track` to actually
change, then posts. See docs/incidents/now-playing-card-download-race.md.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from poob.discord_bot.agent_handler import _post_now_playing_when_ready


class _Track:
    def __init__(self, title: str) -> None:
        self.title = title


class _Player:
    """current_track follows a scripted sequence, one step per read."""

    def __init__(self, values: list) -> None:
        self._values = values
        self._calls = 0

    @property
    def current_track(self):
        v = self._values[min(self._calls, len(self._values) - 1)]
        self._calls += 1
        return v


def _cog(values: list, payload=("EMBED", "VIEW")):
    cog = MagicMock()
    cog._get_player.return_value = _Player(values)
    cog.build_now_playing_message.return_value = payload
    return cog


@pytest.mark.asyncio
async def test_posts_when_track_starts_after_download_delay() -> None:
    """current_track is None for a couple polls (downloading), then flips to
    the new track → card posts once it's actually playing."""
    track = _Track("A Hood Classic")
    cog = _cog([None, None, track])
    channel = MagicMock()
    channel.send = AsyncMock()

    with patch("poob.discord_bot.agent_handler.asyncio.sleep", new=AsyncMock()):
        await _post_now_playing_when_ready(
            cog, channel, guild_id=10, track_before=None, user_id="u1",
            timeout=5.0, interval=0.25,
        )

    channel.send.assert_awaited_once()
    _, kw = channel.send.call_args
    assert kw["embed"] == "EMBED" and kw["view"] == "VIEW"


@pytest.mark.asyncio
async def test_no_post_for_queue_only_request() -> None:
    """A queued track doesn't change `current` → times out silently, no card."""
    playing = _Track("Hood Classic")  # already current, stays current
    cog = _cog([playing])
    channel = MagicMock()
    channel.send = AsyncMock()

    with patch("poob.discord_bot.agent_handler.asyncio.sleep", new=AsyncMock()):
        await _post_now_playing_when_ready(
            cog, channel, guild_id=10, track_before=playing, user_id="u1",
            timeout=0.5, interval=0.25,
        )

    channel.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_post_when_nothing_ever_plays() -> None:
    """current stays None (play failed / nothing started) → no card."""
    cog = _cog([None])
    channel = MagicMock()
    channel.send = AsyncMock()

    with patch("poob.discord_bot.agent_handler.asyncio.sleep", new=AsyncMock()):
        await _post_now_playing_when_ready(
            cog, channel, guild_id=10, track_before=None, user_id="u1",
            timeout=0.5, interval=0.25,
        )

    channel.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_card_when_builder_returns_none() -> None:
    """Track changed but the embed builder declines (None) → no send, no crash."""
    track = _Track("X")
    cog = _cog([track], payload=None)
    channel = MagicMock()
    channel.send = AsyncMock()

    with patch("poob.discord_bot.agent_handler.asyncio.sleep", new=AsyncMock()):
        await _post_now_playing_when_ready(
            cog, channel, guild_id=10, track_before=None, user_id="u1",
            timeout=5.0, interval=0.25,
        )

    channel.send.assert_not_awaited()
