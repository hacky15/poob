"""Tests for auto-join clearing a stale/zombie voice connection.

Incident 2026-06-17: a torn-down session left ``guild.voice_client`` half-dead
— ``is_connected()`` returned False, but Discord's gateway still held the
connection. ``_auto_join_requester_vc`` called ``channel.connect()`` with no
guard, so Discord raised "Already connected to a voice channel" → every
music-triggered auto-join failed → Poob could not join ANY voice channel
("you gotta be in a voice channel for me to play anything"). ``/join`` survived
because it force-disconnects first (``VoiceCog._force_disconnect``); auto-join
did not. Fix: auto-join force-disconnects any lingering voice client before
connecting.

See docs/incidents/zombie-voice-connection-blocks-autojoin.md.
"""

from __future__ import annotations

import inspect
from unittest.mock import AsyncMock, MagicMock

import pytest

from poob.discord_bot.cogs.music_cog import MusicCog


def _cog() -> MusicCog:
    cog = MusicCog.__new__(MusicCog)
    cog._setup_voice_session = None  # skip the listening-setup branch
    return cog


def _guild_with_member_in_channel() -> tuple[MagicMock, MagicMock]:
    channel = MagicMock()
    channel.connect = AsyncMock(return_value=MagicMock(name="new_vc"))
    member = MagicMock()
    member.voice = MagicMock()
    member.voice.channel = channel
    guild = MagicMock()
    guild.id = 702353477602377769
    guild.get_member.return_value = member
    return guild, channel


@pytest.mark.asyncio
async def test_auto_join_force_disconnects_stale_voice_client() -> None:
    """The exact prod zombie: voice_client present but half-dead. Auto-join must
    force-disconnect it before connecting, or channel.connect() raises
    'Already connected' and the join fails."""
    cog = _cog()
    guild, channel = _guild_with_member_in_channel()
    stale = MagicMock()
    stale.is_connected.return_value = False  # half-dead zombie
    stale.disconnect = AsyncMock()
    guild.voice_client = stale

    vc = await cog._auto_join_requester_vc(guild, 478697650556895251)

    stale.disconnect.assert_awaited_once_with(force=True)  # zombie cleared first
    channel.connect.assert_awaited_once()                  # then connected fresh
    assert vc is not None


@pytest.mark.asyncio
async def test_auto_join_clean_slate_connects_directly() -> None:
    cog = _cog()
    guild, channel = _guild_with_member_in_channel()
    guild.voice_client = None  # nothing to clear

    vc = await cog._auto_join_requester_vc(guild, 123)

    channel.connect.assert_awaited_once()
    assert vc is not None


@pytest.mark.asyncio
async def test_auto_join_returns_none_when_requester_not_in_vc() -> None:
    """Invariant preserved: if the requester isn't in a VC, return None (caller
    text-replies 'hop in')."""
    cog = _cog()
    guild = MagicMock()
    guild.id = 1
    member = MagicMock()
    member.voice = None
    guild.get_member.return_value = member
    guild.voice_client = None

    vc = await cog._auto_join_requester_vc(guild, 123)
    assert vc is None


def test_auto_join_source_force_disconnects_before_connect() -> None:
    """Grep-as-test: lock the force-disconnect guard so a future refactor can't
    silently drop it and reintroduce the zombie-blocks-autojoin bug."""
    src = inspect.getsource(MusicCog._auto_join_requester_vc)
    assert "disconnect" in src and "force=True" in src, (
        "auto-join must force-disconnect a lingering voice client before "
        "channel.connect() — see "
        "docs/incidents/zombie-voice-connection-blocks-autojoin.md"
    )
