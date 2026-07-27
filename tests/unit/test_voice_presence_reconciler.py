"""Tests for the voice-presence reconciler — re-establishing a VC membership
that died mid-flight, between restarts.

Incident 2026-07-21: guild 702353477602377769 hit voice WS 1006 at 05:47:07,
py-cord's internal retry loop exhausted ("Could not connect to voice...
Retrying..." → normal 1000 close), and poob never came back. The persisted
membership in ``data/voice_state.json`` still said it should be in that
channel, but nothing ever compared that intent against reality again:
``restore_sessions()`` is ``_restored``-guarded and runs once from
``on_ready``. Poob stayed deaf and mute in that guild for 73+ hours, with no
error surfaced, self-healing only on the next redeploy.

[[voice-auto-rejoin-on-restart]] designed the store to "keep persisted and
retry on the next restart" — this closes the gap where there IS no next
restart. See docs/incidents/voice-session-never-reestablished-mid-flight.md.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from poob.discord_bot.cogs.voice_cog import VoiceCog

GUILD = 702353477602377769
CHANNEL = 889223647674384414


def _cog(pairs: list[tuple[int, int]] | None = None) -> VoiceCog:
    """A VoiceCog with __init__ skipped and only the collaborators the
    reconciler touches wired up."""
    cog = VoiceCog.__new__(VoiceCog)
    cog.bot = MagicMock()
    cog._sessions = {}
    cog._restored = True
    cog._state_store = MagicMock()
    cog._state_store.load.return_value = pairs if pairs is not None else [(GUILD, CHANNEL)]
    cog._reconcile_attempts = {}
    return cog


def _voice_channel(guild: MagicMock) -> MagicMock:
    """A discord.VoiceChannel-shaped mock that passes the isinstance gate."""
    import discord

    channel = MagicMock(spec=discord.VoiceChannel)
    channel.id = CHANNEL
    channel.name = "Kittens Playhouse"
    channel.guild = guild
    channel.connect = AsyncMock(return_value=MagicMock(name="new_vc"))
    return channel


def _guild(voice_client: MagicMock | None = None) -> MagicMock:
    guild = MagicMock()
    guild.id = GUILD
    guild.voice_client = voice_client
    return guild


@pytest.mark.asyncio
async def test_reconciler_reestablishes_a_session_that_died_mid_flight() -> None:
    """The exact prod outage: membership persisted, no live session, no
    zombie client — the reconciler must connect and re-run the standard
    setup chokepoint."""
    cog = _cog()
    guild = _guild(voice_client=None)
    channel = _voice_channel(guild)
    cog.bot.get_channel.return_value = channel
    cog.setup_session_for_vc = AsyncMock(return_value=MagicMock(name="session"))
    cog._force_disconnect = AsyncMock()

    await cog._reconcile_voice_presence_once()

    channel.connect.assert_awaited_once()
    cog.setup_session_for_vc.assert_awaited_once()
    # A restored session must not announce itself, same as boot restore.
    assert cog.setup_session_for_vc.await_args.kwargs["play_entrance"] is False
    cog._state_store.forget.assert_not_called()


@pytest.mark.asyncio
async def test_reconciler_leaves_a_healthy_session_alone() -> None:
    """A live, connected session must never be disturbed — no disconnect,
    no reconnect, no setup."""
    cog = _cog()
    live = MagicMock()
    live.voice_client.is_connected.return_value = True
    cog._sessions[GUILD] = live
    guild = _guild(voice_client=live.voice_client)
    channel = _voice_channel(guild)
    cog.bot.get_channel.return_value = channel
    cog.setup_session_for_vc = AsyncMock()
    cog._force_disconnect = AsyncMock()

    await cog._reconcile_voice_presence_once()

    channel.connect.assert_not_awaited()
    cog.setup_session_for_vc.assert_not_awaited()
    cog._force_disconnect.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconciler_clears_a_zombie_client_without_forgetting_membership() -> None:
    """Zombie guard (2026-06-17 incident): a half-dead voice_client makes
    channel.connect() raise 'Already connected', so it must be cleared first.

    The trap this pins: _force_disconnect's DEFAULT also prunes the store —
    using it naively here would erase the very membership the reconciler
    exists to restore, turning a recoverable outage into a permanent one."""
    cog = _cog()
    zombie = MagicMock()
    zombie.is_connected.return_value = False
    guild = _guild(voice_client=zombie)
    channel = _voice_channel(guild)
    cog.bot.get_channel.return_value = channel
    cog.setup_session_for_vc = AsyncMock(return_value=MagicMock(name="session"))
    cog._force_disconnect = AsyncMock()

    await cog._reconcile_voice_presence_once()

    cog._force_disconnect.assert_awaited_once()
    assert cog._force_disconnect.await_args.kwargs["forget"] is False
    channel.connect.assert_awaited_once()
    cog._state_store.forget.assert_not_called()


@pytest.mark.asyncio
async def test_force_disconnect_still_prunes_the_store_by_default() -> None:
    """Regression guard for /leave and the everyone-left auto-disconnect:
    the forget=False escape hatch must not change the default."""
    cog = _cog()
    cog._state_store = MagicMock()
    cog._sessions = {}
    guild = _guild(voice_client=None)

    await cog._force_disconnect(guild)
    cog._state_store.forget.assert_called_once_with(GUILD)

    cog._state_store.reset_mock()
    await cog._force_disconnect(guild, forget=False)
    cog._state_store.forget.assert_not_called()


@pytest.mark.asyncio
async def test_reconciler_forgets_a_channel_that_is_gone() -> None:
    """A deleted channel is permanent — stop retrying it forever."""
    cog = _cog()
    cog.bot.get_channel.return_value = None
    cog.setup_session_for_vc = AsyncMock()

    await cog._reconcile_voice_presence_once()

    cog._state_store.forget.assert_called_once_with(GUILD)
    cog.setup_session_for_vc.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconciler_keeps_membership_and_never_raises_on_transient_failure() -> None:
    """A failed connect must be swallowed (the loop must survive) and the
    membership kept, per [[voice-auto-rejoin-on-restart]]'s stated policy."""
    cog = _cog()
    guild = _guild(voice_client=None)
    channel = _voice_channel(guild)
    channel.connect = AsyncMock(side_effect=RuntimeError("handshake timeout"))
    cog.bot.get_channel.return_value = channel
    cog.setup_session_for_vc = AsyncMock()
    cog._force_disconnect = AsyncMock()

    await cog._reconcile_voice_presence_once()  # must not raise

    cog._state_store.forget.assert_not_called()


@pytest.mark.asyncio
async def test_reconciler_disconnects_when_setup_fails_but_keeps_membership() -> None:
    """Mirrors boot restore: a connected-but-unsetup VC is worse than none
    (it looks joined but cannot hear), so drop it — and retry next cycle."""
    cog = _cog()
    guild = _guild(voice_client=None)
    channel = _voice_channel(guild)
    new_vc = MagicMock()
    new_vc.disconnect = AsyncMock()
    channel.connect = AsyncMock(return_value=new_vc)
    cog.bot.get_channel.return_value = channel
    cog.setup_session_for_vc = AsyncMock(return_value=None)  # setup failed
    cog._force_disconnect = AsyncMock()

    await cog._reconcile_voice_presence_once()

    new_vc.disconnect.assert_awaited_once()
    cog._state_store.forget.assert_not_called()


@pytest.mark.asyncio
async def test_reconciler_is_a_noop_when_nothing_is_persisted() -> None:
    cog = _cog(pairs=[])
    cog.setup_session_for_vc = AsyncMock()
    cog._force_disconnect = AsyncMock()

    await cog._reconcile_voice_presence_once()

    cog.setup_session_for_vc.assert_not_awaited()
    cog._force_disconnect.assert_not_awaited()


def test_reconciler_loop_is_wired_and_stoppable() -> None:
    """The loop must exist with a sane interval and be stopped on unload —
    a task that outlives the cog would reconnect into a dead bot."""
    assert VoiceCog.VOICE_RECONCILE_INTERVAL_SEC >= 30
    cog = _cog()
    assert hasattr(cog, "_reconcile_voice_presence")
    cog._reconcile_voice_presence = MagicMock()
    cog._stop_tasks()
    cog._reconcile_voice_presence.cancel.assert_called_once()
