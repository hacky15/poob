"""End-to-end tests for AgentMessageHandler's music-reply flow.

Pins the three behaviors from the 2026-07-12 report ("@Poob play backwoods
808 fishing" → Poob-styled reply, Poob's voice in VC, no now-playing card):

1. The VOICE_TOOB sentinel on a music reply is stripped from the POSTED text.
2. The VC speak call receives persona="toob" so Toob's voice speaks it.
3. The now-playing snapshot (`track_before`) is taken BEFORE the brain call —
   the brain call is what starts the track, so an after-call snapshot sees
   the new track as baseline and the card never posts. See
   docs/incidents/now-playing-card-snapshot-after-brain-call.md.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from poob.brain.poob import VOICE_TOOB
from poob.discord_bot.agent_handler import AgentMessageHandler


class _Player:
    def __init__(self) -> None:
        self.current_track = None


class _Track:
    title = "Draggin Bottom"


def _flow_fixture():
    """(handler, message, brain, music_cog, voice_cog, player) fully wired
    so on_message runs end-to-end with no network / no Discord."""
    bot = MagicMock()
    bot.command_prefix = "!"
    bot.user = MagicMock()
    bot.user.id = 999

    brain = MagicMock()
    handler = AgentMessageHandler(bot, brain)
    handler._fetch_channel_context = AsyncMock(return_value="")  # type: ignore[method-assign]

    player = _Player()
    music_cog = MagicMock()
    music_cog._get_player.return_value = player

    voice_cog = MagicMock()
    voice_cog.speak_if_in_channel = AsyncMock(return_value=True)

    bot.get_cog.side_effect = lambda name: {"Music": music_cog, "Voice": voice_cog}[name]

    message = MagicMock()
    message.author.bot = False
    message.author.id = 42
    message.author.display_name = "Ben"
    message.content = "<@999> play backwoods 808 fishing"
    message.mentions = [bot.user]
    message.guild.id = 10
    message.reply = AsyncMock()

    typing_cm = MagicMock()
    typing_cm.__aenter__ = AsyncMock()
    typing_cm.__aexit__ = AsyncMock(return_value=False)
    message.channel.typing.return_value = typing_cm

    return handler, message, brain, music_cog, voice_cog, player


@pytest.mark.asyncio
async def test_music_reply_strips_sentinel_and_speaks_as_toob() -> None:
    handler, message, brain, _music, voice_cog, player = _flow_fixture()

    async def _respond(*a, **kw):  # noqa: ANN002, ANN003
        player.current_track = _Track()  # brain side effect: track starts
        return VOICE_TOOB + "may this lake swallow you whole"

    brain.respond = AsyncMock(side_effect=_respond)

    with patch(
        "poob.discord_bot.agent_handler._post_now_playing_when_ready",
        new=AsyncMock(),
    ):
        await handler.on_message(message)
        await asyncio.sleep(0)

    # 1. Posted text carries NO sentinel.
    posted = message.reply.call_args.args[0]
    assert posted == "may this lake swallow you whole"
    assert VOICE_TOOB not in posted

    # 2. Spoken in VC with Toob's persona.
    voice_cog.speak_if_in_channel.assert_awaited_once()
    call = voice_cog.speak_if_in_channel.await_args
    assert call.args[1] == "may this lake swallow you whole"
    assert call.kwargs.get("persona") == "toob"


@pytest.mark.asyncio
async def test_now_playing_snapshot_taken_before_brain_call() -> None:
    """The card-killer: the track becomes `current` DURING the brain call.
    track_before must be the PRE-call value (None here), or the poll waits
    for a change that already happened and the card never posts."""
    handler, message, brain, music_cog, _voice, player = _flow_fixture()

    async def _respond(*a, **kw):  # noqa: ANN002, ANN003
        player.current_track = _Track()  # download finished mid-brain-call
        return VOICE_TOOB + "venom"

    brain.respond = AsyncMock(side_effect=_respond)

    with patch(
        "poob.discord_bot.agent_handler._post_now_playing_when_ready",
        new=AsyncMock(),
    ) as poster:
        await handler.on_message(message)
        await asyncio.sleep(0)

    poster.assert_awaited_once()
    args = poster.await_args.args
    # (music_cog, channel, guild_id, track_before, user_id)
    assert args[0] is music_cog
    assert args[2] == 10
    assert args[3] is None, "track_before must be the PRE-brain-call snapshot"


@pytest.mark.asyncio
async def test_plain_chat_reply_stays_poob() -> None:
    handler, message, brain, _music, voice_cog, _player = _flow_fixture()
    brain.respond = AsyncMock(return_value="nah man that movie was mid")

    with patch(
        "poob.discord_bot.agent_handler._post_now_playing_when_ready",
        new=AsyncMock(),
    ):
        await handler.on_message(message)
        await asyncio.sleep(0)

    posted = message.reply.call_args.args[0]
    assert posted == "nah man that movie was mid"
    call = voice_cog.speak_if_in_channel.await_args
    assert call.kwargs.get("persona") == "poob"
