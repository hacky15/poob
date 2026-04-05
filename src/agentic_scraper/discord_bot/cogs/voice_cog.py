"""Voice chat cog — Discord VC integration for Poob (Pycord native).

Commands: !join, !leave, !say, !voice.
Uses Pycord's native start_recording() + custom RealtimeAudioSink for
real-time voice receive with DAVE E2EE support.

Stage channel support: auto-promotes bot and users to Speaker.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Callable

import discord
from discord.ext import commands, tasks

from agentic_scraper.utils.logging import get_logger
from agentic_scraper.voice.realtime_sink import RealtimeAudioSink

if TYPE_CHECKING:
    from agentic_scraper.voice.session import VoiceSession

# Type alias for the session factory callable
SessionFactory = Callable[[discord.VoiceClient], "VoiceSession"]

log = get_logger("discord.voice_cog")


class VoiceCog(commands.Cog, name="Voice"):
    """Voice channel integration using Pycord native recording.

    Joins voice channels and speaks responses via TTS. Supports
    bidirectional voice (STT → LLM → TTS) via Pycord's start_recording()
    with DAVE E2EE handled by voice_compat patches.
    """

    def __init__(
        self,
        bot: commands.Bot,
        session_factory: SessionFactory,
    ) -> None:
        self.bot = bot
        self._session_factory = session_factory
        self._sessions: dict[int, VoiceSession] = {}  # guild_id → session
        self._text_to_voice: set[int] = set()  # guild_ids with text-to-voice enabled

    def _stop_tasks(self) -> None:
        """Stop background tasks."""
        pass

    async def _force_disconnect(self, guild: discord.Guild) -> None:
        """Force-disconnect any existing voice client for this guild."""
        guild_id = guild.id
        self._text_to_voice.discard(guild_id)

        if guild_id in self._sessions:
            session = self._sessions.pop(guild_id)
            await session.cleanup()
            try:
                vc = session.voice_client
                if vc.is_connected():
                    if getattr(vc, "recording", False):
                        vc.stop_recording()
                    await vc.disconnect(force=True)
            except Exception:
                pass

        if guild.voice_client is not None:
            try:
                await guild.voice_client.disconnect(force=True)
            except Exception:
                pass

    def _get_session(self, guild_id: int) -> VoiceSession | None:
        """Get an active voice session for a guild, or None."""
        session = self._sessions.get(guild_id)
        if session and session.voice_client.is_connected():
            return session
        return None

    async def _promote_to_speaker(self, guild: discord.Guild) -> None:
        """Promote the bot to Speaker in a stage channel."""
        me = guild.me
        if me and me.voice and me.voice.suppress:
            try:
                await me.edit(suppress=False)
                log.info("Bot promoted to stage speaker", guild=guild.id)
            except discord.Forbidden:
                log.warning("No permission to promote self to speaker")
            except Exception as exc:
                log.warning("Failed to promote to speaker", error=str(exc)[:80])

    async def _promote_member(self, member: discord.Member) -> None:
        """Auto-promote a member to Speaker in a stage channel."""
        if member.bot:
            return
        if member.voice and member.voice.suppress:
            try:
                await member.edit(suppress=False)
                log.info("Auto-promoted user to speaker", user=member.id)
            except (discord.Forbidden, Exception):
                pass

    def _is_stage_channel(self, channel: discord.abc.Connectable) -> bool:
        """Check if a channel is a stage channel."""
        return isinstance(channel, discord.StageChannel)

    @commands.command(name="join", aliases=["vc"])
    async def join_voice(self, ctx: commands.Context) -> None:  # type: ignore[type-arg]
        """Join your voice channel. Bot speaks responses via TTS.

        Works in both regular voice channels and stage channels.
        Usage: !join or !vc
        """
        if not ctx.author.voice or not ctx.author.voice.channel:  # type: ignore[union-attr]
            await ctx.send("You need to be in a voice channel first.")
            return

        channel = ctx.author.voice.channel  # type: ignore[union-attr]
        guild = ctx.guild  # type: ignore[union-attr]
        guild_id = guild.id
        is_stage = self._is_stage_channel(channel)

        # Already in this exact channel?
        if guild_id in self._sessions:
            existing = self._sessions[guild_id]
            if existing.voice_client.is_connected():
                if existing.voice_client.channel == channel:
                    await ctx.send("I'm already in your channel.")
                    return

        await self._force_disconnect(guild)
        await asyncio.sleep(0.5)

        try:
            # Connect to voice channel
            vc = await channel.connect(timeout=15.0)
            log.info("Connected to voice channel", stage=is_stage)

            if is_stage:
                # Stage channel setup
                stage_instance = getattr(channel, "instance", None)
                if stage_instance is None:
                    try:
                        await channel.create_instance(topic="Poob Voice Chat")
                    except (discord.Forbidden, discord.HTTPException):
                        pass
                await asyncio.sleep(0.5)
                await self._promote_to_speaker(guild)
                if hasattr(ctx.author, "edit"):
                    await self._promote_member(ctx.author)  # type: ignore[arg-type]

            # Create voice session
            session = self._session_factory(vc)
            session.is_stage = is_stage
            session._last_utterance_time = time.monotonic()
            self._sessions[guild_id] = session

            # Wait for DAVE E2EE handshake
            dave_ready = False
            dave_version = getattr(vc, "dave_protocol_version", 0) or 0
            if dave_version > 0:
                for _ in range(50):  # 5 second timeout
                    dave_session = getattr(vc, "dave_session", None)
                    if dave_session and getattr(dave_session, "ready", False):
                        log.info("DAVE ready, starting audio receive")
                        dave_ready = True
                        break
                    await asyncio.sleep(0.1)
                if not dave_ready:
                    log.warning("DAVE not ready after 5s, starting anyway")
            else:
                log.info("No DAVE negotiated (version=0)")

            # Start recording with our real-time sink
            def on_audio_frame(user_id: int, pcm_data: bytes) -> None:
                """Called from Pycord's recording thread for each decoded frame."""
                if not session.is_listening:
                    return
                # Route through session's unified audio processor.
                # If dual pipeline is active, feeds Porcupine + Deepgram.
                # Otherwise falls back to energy-based VAD + batch STT.
                session.process_audio_frame(user_id, pcm_data)

            # Build sink with appropriate stale-buffer detection
            if session.uses_dual_pipeline:
                sink = RealtimeAudioSink(
                    on_audio_frame=on_audio_frame,
                    on_stale_check=lambda: session._dual_pipeline.check_stale_buffers(),
                )
            else:
                sink = RealtimeAudioSink(
                    on_audio_frame=on_audio_frame,
                    get_buffers=lambda: session._user_buffers,
                )

            async def on_recording_stop(sink_obj: discord.sinks.Sink, *args) -> None:
                """Called when recording stops."""
                log.debug("Recording stopped")

            vc.start_recording(sink, on_recording_stop)
            log.info(
                "Recording started with RealtimeAudioSink",
                dave=dave_ready,
                dave_version=dave_version,
            )

            # Enable text-to-voice mode
            self._text_to_voice.add(guild_id)

            await ctx.send(
                f"Joined **{channel.name}** and listening!\n"
                f"I can hear you — talk and I'll respond.\n"
                f"@mention me or type while in VC to chat. `!leave` to disconnect."
            )

            log.info(
                "Joined voice channel",
                guild=guild_id,
                channel=channel.name,
                stage=is_stage,
            )

        except Exception as exc:
            await self._force_disconnect(guild)
            log.error("Failed to join voice", error=str(exc)[:120])
            await ctx.send(f"Failed to join voice channel: {exc}")

    @commands.command(name="leave", aliases=["dc"])
    async def leave_voice(self, ctx: commands.Context) -> None:  # type: ignore[type-arg]
        """Leave the current voice channel.

        Usage: !leave or !dc
        """
        guild = ctx.guild  # type: ignore[union-attr]

        if guild.id not in self._sessions and guild.voice_client is None:
            await ctx.send("I'm not in a voice channel.")
            return

        await self._force_disconnect(guild)
        await ctx.send("Left voice channel. Later!")
        log.info("Left voice channel", guild=guild.id)

    @commands.command(name="say")
    async def say_text(self, ctx: commands.Context, *, text: str) -> None:  # type: ignore[type-arg]
        """Make the bot say something in voice chat via TTS.

        Usage: !say Hello, how are you?
        """
        guild_id = ctx.guild.id  # type: ignore[union-attr]
        session = self._get_session(guild_id)

        if not session:
            await ctx.send("I'm not in a voice channel. Use `!join` first.")
            return

        await ctx.message.add_reaction("\U0001F50A")  # speaker emoji

        try:
            audio = await session._synthesize(text)
            if audio:
                await session._play_audio(audio)
                log.info("TTS played", text=text[:80], guild=guild_id)
            else:
                await ctx.send("TTS failed — no audio generated.")
        except Exception as exc:
            log.error("TTS playback failed", error=str(exc)[:100])
            await ctx.send(f"TTS error: {exc}")

    @commands.command(name="voice")
    async def toggle_voice(self, ctx: commands.Context) -> None:  # type: ignore[type-arg]
        """Toggle text-to-voice mode on/off.

        When on, messages in this channel get spoken aloud in VC.
        Usage: !voice
        """
        guild_id = ctx.guild.id  # type: ignore[union-attr]

        if guild_id not in self._sessions:
            await ctx.send("I'm not in a voice channel. Use `!join` first.")
            return

        if guild_id in self._text_to_voice:
            self._text_to_voice.discard(guild_id)
            await ctx.send("Text-to-voice **disabled**. Use `!say` for manual TTS.")
        else:
            self._text_to_voice.add(guild_id)
            await ctx.send("Text-to-voice **enabled**. Type here and I'll respond in VC!")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """Respond to text messages with voice when in text-to-voice mode."""
        if message.author.bot:
            return
        if not message.guild:
            return
        if message.content.startswith(self.bot.command_prefix):  # type: ignore[arg-type]
            return

        guild_id = message.guild.id
        if guild_id not in self._text_to_voice:
            return

        session = self._get_session(guild_id)
        if not session:
            return

        is_mention = self.bot.user in message.mentions if self.bot.user else False
        user_in_vc = False
        if hasattr(message.author, "voice") and message.author.voice:  # type: ignore[union-attr]
            user_vc = message.author.voice.channel  # type: ignore[union-attr]
            if user_vc and session.voice_client.channel == user_vc:
                user_in_vc = True

        if not is_mention and not user_in_vc:
            return

        text = message.content
        if self.bot.user:
            text = text.replace(f"<@{self.bot.user.id}>", "").strip()
            text = text.replace(f"<@!{self.bot.user.id}>", "").strip()

        if not text:
            return

        log.info("Text-to-voice triggered", user=message.author.id, text=text[:80])

        try:
            async with message.channel.typing():
                response = await session.brain.respond(
                    message=text,
                    user_id=str(message.author.id),
                    channel_id=str(message.channel.id),
                    voice=True,
                )

            if not response:
                return

            await message.reply(response, mention_author=False)

            audio = await session._synthesize(response)
            if audio:
                await session._play_audio(audio)
                log.info("Voice response played", user=message.author.id)

        except Exception as exc:
            log.error("Text-to-voice failed", error=str(exc)[:120])

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        """Auto-leave if alone; auto-promote users in stage channels."""
        guild_id = member.guild.id

        if guild_id not in self._sessions:
            return

        session = self._sessions[guild_id]
        if not session.voice_client.is_connected():
            return

        vc_channel = session.voice_client.channel
        if vc_channel is None:
            return

        # Auto-promote users joining a stage channel
        if (
            getattr(session, "is_stage", False)
            and after.channel == vc_channel
            and not member.bot
        ):
            await self._promote_member(member)

        # Auto-leave if bot is alone
        human_members = [m for m in vc_channel.members if not m.bot]
        if len(human_members) == 0:
            log.info("All users left voice, auto-disconnecting", guild=guild_id)
            await self._force_disconnect(member.guild)

    async def cog_unload(self) -> None:
        """Clean up all sessions when cog is unloaded."""
        for guild_id, session in list(self._sessions.items()):
            await session.cleanup()
            if session.voice_client.is_connected():
                if getattr(session.voice_client, "recording", False):
                    session.voice_client.stop_recording()
                await session.voice_client.disconnect()
        self._sessions.clear()
        self._text_to_voice.clear()
