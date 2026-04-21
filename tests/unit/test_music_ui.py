"""Unit tests for music UI + VoiceCog voice-gate + wake-word dual-gate.

Covers the April 21 2026 one-handler refactor:

- ``MusicControlsView`` dispatches button clicks via the same
  ``handle_music_request(..., tool_args={...})`` contract voice/text use.
- ``build_now_playing_embed`` renders the standard post-Rythm card.
- ``VoiceCog.speak_if_in_channel`` speaks iff author shares Poob's VC.
- ``DualPipelineProcessor._emit_utterance`` requires BOTH acoustic and
  text confirmation (rejects Deepgram mic-loopback hallucinations).
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest


# ---------------------------------------------------------------------------
# build_now_playing_embed
# ---------------------------------------------------------------------------


class TestNowPlayingEmbed:
    def _make_track(self, **kw):
        from poob.music.queue import Track

        defaults = dict(
            title="Pinball Wizard",
            url="https://youtu.be/abc123",
            duration=timedelta(seconds=185),
            requester_id=1,
            requester_name="Ben",
            thumbnail="https://img.example/thumb.jpg",
        )
        defaults.update(kw)
        return Track(**defaults)

    def test_renders_core_fields(self) -> None:
        from poob.discord_bot.music_ui import build_now_playing_embed

        track = self._make_track()
        embed = build_now_playing_embed(track, queue_size=2)

        assert embed.title == "Pinball Wizard"
        assert embed.url == "https://youtu.be/abc123"
        assert embed.thumbnail.url == "https://img.example/thumb.jpg"

        field_names = [f.name for f in embed.fields]
        assert "Duration" in field_names
        assert "Up Next" in field_names
        assert "2 tracks queued" in [f.value for f in embed.fields if f.name == "Up Next"][0]

        assert embed.footer.text == "Requested by Ben"

    def test_omits_up_next_when_queue_empty(self) -> None:
        from poob.discord_bot.music_ui import build_now_playing_embed

        track = self._make_track()
        embed = build_now_playing_embed(track, queue_size=0)

        field_names = [f.name for f in embed.fields]
        assert "Up Next" not in field_names

    def test_missing_thumbnail_does_not_crash(self) -> None:
        from poob.discord_bot.music_ui import build_now_playing_embed

        track = self._make_track(thumbnail=None)
        embed = build_now_playing_embed(track, queue_size=0)
        # Pycord returns an _EmbedMediaProxy with url=None when unset,
        # or None directly when no thumbnail was ever assigned.
        thumb = embed.thumbnail
        assert thumb is None or thumb.url is None

    def test_livestream_duration(self) -> None:
        from poob.discord_bot.music_ui import build_now_playing_embed

        track = self._make_track(duration=None, is_stream=True)
        embed = build_now_playing_embed(track, queue_size=0)
        dur_field = next(f for f in embed.fields if f.name == "Duration")
        assert dur_field.value == "LIVE"


# ---------------------------------------------------------------------------
# MusicControlsView — buttons funnel to handle_music_request
# ---------------------------------------------------------------------------


class TestMusicControlsView:
    """Buttons must produce the same tool_args shape the LLM emits."""

    def _make_interaction(self, guild_id: int = 42, user_id: int = 7):
        """Build a minimal mock discord.Interaction."""
        interaction = MagicMock()
        interaction.guild = MagicMock()
        interaction.guild.id = guild_id
        interaction.user = MagicMock()
        interaction.user.id = user_id
        interaction.response = MagicMock()
        interaction.response.defer = AsyncMock()
        interaction.response.send_message = AsyncMock()
        interaction.followup = MagicMock()
        interaction.followup.send = AsyncMock()
        return interaction

    async def test_skip_button_builds_structured_tool_args(self) -> None:
        from poob.discord_bot.music_ui import MusicControlsView

        handler = AsyncMock(return_value="[SILENT]Skipped Song X.")
        view = MusicControlsView(music_handler=handler)
        interaction = self._make_interaction()

        await view._dispatch(interaction, "skip")

        handler.assert_awaited_once()
        kwargs = handler.await_args.kwargs
        assert kwargs["tool_args"] == {"action": "skip"}
        assert kwargs["user_id"] == 7
        assert kwargs["guild_id"] == 42
        assert kwargs["voice"] is False

    async def test_volume_button_includes_value(self) -> None:
        from poob.discord_bot.music_ui import MusicControlsView

        handler = AsyncMock(return_value="[SILENT]Music volume set to 75%.")
        view = MusicControlsView(music_handler=handler)
        interaction = self._make_interaction()

        await view._dispatch(interaction, "volume", value=75)

        kwargs = handler.await_args.kwargs
        assert kwargs["tool_args"] == {"action": "volume", "value": 75}

    async def test_silent_prefix_stripped_in_ephemeral_reply(self) -> None:
        from poob.discord_bot.music_ui import MusicControlsView

        handler = AsyncMock(return_value="[SILENT]Skipped Song X.")
        view = MusicControlsView(music_handler=handler)
        interaction = self._make_interaction()

        await view._dispatch(interaction, "skip")

        interaction.followup.send.assert_awaited_once()
        args, kwargs = interaction.followup.send.await_args
        assert "[SILENT]" not in args[0]
        assert "Skipped Song X" in args[0]
        assert kwargs.get("ephemeral") is True

    async def test_no_handler_sends_error(self) -> None:
        from poob.discord_bot.music_ui import MusicControlsView

        view = MusicControlsView(music_handler=None)
        interaction = self._make_interaction()

        await view._dispatch(interaction, "skip")

        interaction.response.send_message.assert_awaited_once()
        # No handler = no defer, so the error path goes through response.send_message.

    async def test_no_guild_sends_error(self) -> None:
        from poob.discord_bot.music_ui import MusicControlsView

        handler = AsyncMock()
        view = MusicControlsView(music_handler=handler)
        interaction = self._make_interaction()
        interaction.guild = None

        await view._dispatch(interaction, "skip")

        handler.assert_not_awaited()
        interaction.response.send_message.assert_awaited_once()


# ---------------------------------------------------------------------------
# VoiceCog.speak_if_in_channel — the VC-membership gate
# ---------------------------------------------------------------------------


class TestSpeakIfInChannel:
    """The gate: speak iff author shares Poob's current voice channel."""

    def _make_cog_with_session(self, bot_channel) -> "tuple":
        """Return (cog, session) with a mock session bound to bot_channel."""
        import discord
        from poob.discord_bot.cogs.voice_cog import VoiceCog

        bot = MagicMock(spec=discord.ext.commands.Bot)
        cog = VoiceCog(bot=bot, session_factory=MagicMock())

        session = MagicMock()
        session.voice_client = MagicMock()
        session.voice_client.is_connected = MagicMock(return_value=True)
        session.voice_client.channel = bot_channel
        session._synthesize = AsyncMock(return_value=b"\x00" * 100)
        session._play_audio = AsyncMock()

        # The cog's _get_session runs `session.voice_client.is_connected()`
        # and returns the session if connected.
        cog._sessions = {99: session}
        return cog, session

    def _make_message(self, guild_id: int, author_channel):
        """Build a minimal mock discord.Message."""
        message = MagicMock()
        message.guild = MagicMock()
        message.guild.id = guild_id
        message.author = MagicMock()
        message.author.id = 1234
        if author_channel is None:
            message.author.voice = None
        else:
            message.author.voice = MagicMock()
            message.author.voice.channel = author_channel
        return message

    async def test_speaks_when_author_in_same_vc(self) -> None:
        bot_channel = MagicMock(name="VC-A")
        cog, session = self._make_cog_with_session(bot_channel)
        message = self._make_message(guild_id=99, author_channel=bot_channel)

        result = await cog.speak_if_in_channel(message, "hello world")

        assert result is True
        session._synthesize.assert_awaited_once_with("hello world")
        session._play_audio.assert_awaited_once()

    async def test_silent_when_author_in_different_vc(self) -> None:
        bot_channel = MagicMock(name="VC-A")
        other_channel = MagicMock(name="VC-B")
        cog, session = self._make_cog_with_session(bot_channel)
        message = self._make_message(guild_id=99, author_channel=other_channel)

        result = await cog.speak_if_in_channel(message, "hello")

        assert result is False
        session._synthesize.assert_not_awaited()

    async def test_silent_when_author_not_in_vc(self) -> None:
        bot_channel = MagicMock(name="VC-A")
        cog, session = self._make_cog_with_session(bot_channel)
        message = self._make_message(guild_id=99, author_channel=None)

        result = await cog.speak_if_in_channel(message, "hello")

        assert result is False
        session._synthesize.assert_not_awaited()

    async def test_silent_when_no_session(self) -> None:
        import discord
        from poob.discord_bot.cogs.voice_cog import VoiceCog

        bot = MagicMock(spec=discord.ext.commands.Bot)
        cog = VoiceCog(bot=bot, session_factory=MagicMock())
        # No session for guild
        message = self._make_message(guild_id=99, author_channel=MagicMock())

        result = await cog.speak_if_in_channel(message, "hello")
        assert result is False

    async def test_silent_when_dm(self) -> None:
        bot_channel = MagicMock(name="VC-A")
        cog, session = self._make_cog_with_session(bot_channel)
        message = MagicMock()
        message.guild = None  # DM
        message.author = MagicMock()

        result = await cog.speak_if_in_channel(message, "hello")

        assert result is False
        session._synthesize.assert_not_awaited()

    async def test_silent_when_text_empty(self) -> None:
        bot_channel = MagicMock(name="VC-A")
        cog, session = self._make_cog_with_session(bot_channel)
        message = self._make_message(guild_id=99, author_channel=bot_channel)

        result = await cog.speak_if_in_channel(message, "   ")

        assert result is False
        session._synthesize.assert_not_awaited()


# ---------------------------------------------------------------------------
# Wake-word dual-gate — both acoustic AND text required
# ---------------------------------------------------------------------------


class TestWakeWordDualGate:
    """``is_addressed = text_match and pipeline.is_active`` — symmetric gate."""

    def _make_processor(self):
        from poob.voice.dual_pipeline import DualPipelineProcessor, UserPipeline

        # Construct without actually initializing network/model components.
        proc = DualPipelineProcessor.__new__(DualPipelineProcessor)
        proc._on_addressed = MagicMock()
        proc._on_passive = MagicMock()
        proc._deepgram = MagicMock()
        proc._deepgram.get_transcript = MagicMock(return_value=("", False))
        proc._deepgram.reset_transcript = MagicMock()
        proc._user_pipelines = {}
        proc._silence_counters = {}
        proc._loop = None
        return proc, UserPipeline

    def _make_pipeline(self, UserPipeline, *, is_active: bool):
        p = UserPipeline(user_id=42, user_name="Ben")
        p.is_active = is_active
        p.speech_started = True
        p.speech_start_time = 1000.0
        return p

    def test_text_and_audio_both_fire_is_addressed(self) -> None:
        proc, UserPipeline = self._make_processor()
        pipeline = self._make_pipeline(UserPipeline, is_active=True)
        proc._deepgram.get_transcript.return_value = (
            "Hey, Poob. Play pinball wizard.", True,
        )

        proc._emit_utterance(42, pipeline)

        proc._on_addressed.assert_called_once()
        proc._on_passive.assert_not_called()

    def test_text_only_no_audio_is_rejected(self) -> None:
        """The April 21 bug: Deepgram hallucinated 'Hey Poob' from loopback
        audio, audio model didn't fire → under old rule, fired anyway.
        New rule: text without acoustic confirmation is passive."""
        proc, UserPipeline = self._make_processor()
        pipeline = self._make_pipeline(UserPipeline, is_active=False)
        proc._deepgram.get_transcript.return_value = (
            "Hey, Poob. Play jah jah jah blah blah blah.", True,
        )

        proc._emit_utterance(42, pipeline)

        proc._on_addressed.assert_not_called()
        proc._on_passive.assert_called_once()

    def test_audio_only_no_text_is_rejected(self) -> None:
        """Audio model false positive without text confirmation."""
        proc, UserPipeline = self._make_processor()
        pipeline = self._make_pipeline(UserPipeline, is_active=True)
        proc._deepgram.get_transcript.return_value = (
            "hey what's up guys", True,
        )

        proc._emit_utterance(42, pipeline)

        proc._on_addressed.assert_not_called()
        proc._on_passive.assert_called_once()

    def test_neither_signal_is_passive(self) -> None:
        proc, UserPipeline = self._make_processor()
        pipeline = self._make_pipeline(UserPipeline, is_active=False)
        proc._deepgram.get_transcript.return_value = (
            "just regular conversation", True,
        )

        proc._emit_utterance(42, pipeline)

        proc._on_addressed.assert_not_called()
        proc._on_passive.assert_called_once()
