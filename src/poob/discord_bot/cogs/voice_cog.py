"""Voice chat cog — Discord VC integration for Poob (Pycord native).

Slash commands: ``/join``, ``/leave`` — the only remaining command surface.
Everything else (TTS, speech responses, music triggers) flows through
natural-language @mentions or voice. Slash commands are the canonical
Discord-UI equivalent of prefix commands and survive the "no commands
with Poob" rule as infrastructure for entering VC.

Uses Pycord's native start_recording() + custom RealtimeAudioSink for
real-time voice receive with DAVE E2EE support.

Stage channel support: auto-promotes bot and users to Speaker.

Voice-output gating: exposes ``speak_if_in_channel(message, text)`` which
AgentMessageHandler calls after it posts a text reply. Speaks via TTS iff
the author is in the voice channel Poob is currently in — no opt-in toggle,
no duplicate ``on_message`` listener.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Callable

import discord
from discord.ext import commands, tasks

from poob.utils.logging import get_logger
from poob.voice.realtime_sink import RealtimeAudioSink

if TYPE_CHECKING:
    from poob.voice.session import VoiceSession

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

    def _stop_tasks(self) -> None:
        """Stop background tasks."""
        pass

    async def _force_disconnect(self, guild: discord.Guild) -> None:
        """Force-disconnect any existing voice client for this guild."""
        guild_id = guild.id

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

    async def setup_session_for_vc(
        self,
        vc: discord.VoiceClient,
        channel: discord.abc.Connectable,
        is_stage: bool = False,
        play_entrance: bool = True,
    ) -> VoiceSession | None:
        """Wire up listening + STT + wake-word for an already-connected VC.

        Used by both ``/join`` (after its own ``channel.connect()``) and
        ``MusicCog._auto_join_requester_vc`` (after auto-connecting for a
        text-channel music request). The ``channel.connect()`` step is
        the caller's responsibility — this helper handles everything
        that comes after: stage promotion, VoiceSession creation,
        DAVE handshake wait, recording sink + start_recording, horniness
        roll. Returns the new session or ``None`` if setup failed.

        ``play_entrance`` is True for ``/join`` (the user expects an
        intro) and False for auto-join from music (the user just wants
        music — an unsolicited "what's up" feels intrusive).
        """
        guild = channel.guild  # type: ignore[union-attr]
        guild_id = guild.id
        try:
            if is_stage:
                stage_instance = getattr(channel, "instance", None)
                if stage_instance is None:
                    try:
                        await channel.create_instance(topic="Poob Voice Chat")  # type: ignore[union-attr]
                    except (discord.Forbidden, discord.HTTPException):
                        pass
                await asyncio.sleep(0.5)
                await self._promote_to_speaker(guild)

            session = self._session_factory(vc)
            session.is_stage = is_stage
            session._last_utterance_time = time.monotonic()
            self._sessions[guild_id] = session

            # Wait for DAVE E2EE handshake to settle. See
            # incidents/dave-timeout-fail-hard-regression for why we
            # proceed regardless of timeout — `dave_session.ready` can
            # stay False while the decoder catches up after first frames.
            dave_ready = False
            dave_version = getattr(vc, "dave_protocol_version", 0) or 0
            DAVE_TIMEOUT_S = 15.0
            DAVE_POLL_S = 0.1
            if dave_version > 0:
                _t_start = time.monotonic()
                while time.monotonic() - _t_start < DAVE_TIMEOUT_S:
                    dave_session = getattr(vc, "dave_session", None)
                    if dave_session and getattr(dave_session, "ready", False):
                        elapsed_ms = int((time.monotonic() - _t_start) * 1000)
                        log.info(
                            "DAVE ready, starting audio receive",
                            elapsed_ms=elapsed_ms,
                        )
                        dave_ready = True
                        break
                    await asyncio.sleep(DAVE_POLL_S)
                if not dave_ready:
                    log.warning(
                        "DAVE not ready within window — starting "
                        "recording anyway; decoder catches up once "
                        "keys arrive.",
                        timeout_s=DAVE_TIMEOUT_S, dave_version=dave_version,
                    )
            else:
                log.info("No DAVE negotiated (version=0)")

            def on_audio_frame(user_id: int, pcm_data: bytes) -> None:
                """Called from Pycord's recording thread for each decoded frame."""
                if not session.is_listening:
                    return
                session.process_audio_frame(user_id, pcm_data)

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
                log.debug("Recording stopped")

            vc.start_recording(sink, on_recording_stop)
            log.info(
                "Recording started with RealtimeAudioSink",
                dave=dave_ready,
                dave_version=dave_version,
                guild=guild_id,
            )

            # Roll horniness for THIS guild. Per-guild keeps each server's
            # vibe independent under the multi-guild isolation contract.
            session.brain.roll_horniness(guild_id)

            if play_entrance:
                await session.play_entrance()

            return session

        except Exception as exc:
            log.error(
                "Failed to set up voice session for VC",
                guild=guild_id, error=str(exc)[:120],
            )
            self._sessions.pop(guild_id, None)
            return None

    @discord.slash_command(name="join", description="Join your voice channel so Poob can listen and speak.")
    async def join_voice(self, ctx: discord.ApplicationContext) -> None:
        """Join the caller's voice channel.

        Works in both regular voice channels and stage channels. This is
        the only way to get Poob into a VC — all @mention / button / voice
        interactions require the bot to already be connected.
        """
        if not ctx.author.voice or not ctx.author.voice.channel:
            await ctx.respond(
                "you gotta be in a voice channel first.", ephemeral=True,
            )
            return

        channel = ctx.author.voice.channel
        guild = ctx.guild
        guild_id = guild.id
        is_stage = self._is_stage_channel(channel)

        # Slash commands must be acknowledged within 3s. The VC connect can
        # take longer, so defer up front and then follow up.
        await ctx.defer()

        # Already in this exact channel?
        if guild_id in self._sessions:
            existing = self._sessions[guild_id]
            if existing.voice_client.is_connected():
                if existing.voice_client.channel == channel:
                    await ctx.followup.send("I'm already in your channel.")
                    return

        await self._force_disconnect(guild)
        await asyncio.sleep(0.5)

        try:
            vc = await channel.connect(timeout=15.0)
            log.info("Connected to voice channel", stage=is_stage)

            # Stage channels need the inviter promoted to speaker too —
            # only ``/join`` knows the inviting member, so this stays here.
            if is_stage and hasattr(ctx.author, "edit"):
                await self._promote_member(ctx.author)  # type: ignore[arg-type]

            session = await self.setup_session_for_vc(
                vc, channel, is_stage=is_stage, play_entrance=False,
            )
            if session is None:
                await self._force_disconnect(guild)
                await ctx.followup.send(
                    "Failed to set up voice session — try again."
                )
                return

            await ctx.followup.send(
                f"Joined **{channel.name}** and listening!\n"
                f"I hear you — talk, @mention, or tap buttons. "
                f"Use `/leave` to disconnect."
            )
            await session.play_entrance()

            log.info(
                "Joined voice channel",
                guild=guild_id,
                channel=channel.name,
                stage=is_stage,
            )

        except Exception as exc:
            await self._force_disconnect(guild)
            log.error("Failed to join voice", error=str(exc)[:120])
            await ctx.followup.send(f"Failed to join voice channel: {exc}")

    @discord.slash_command(name="leave", description="Disconnect Poob from voice.")
    async def leave_voice(self, ctx: discord.ApplicationContext) -> None:
        """Leave the current voice channel."""
        guild = ctx.guild

        if guild.id not in self._sessions and guild.voice_client is None:
            await ctx.respond("I'm not in a voice channel.", ephemeral=True)
            return

        await ctx.defer()
        await self._force_disconnect(guild)
        await ctx.followup.send("Left voice channel. Later!")
        log.info("Left voice channel", guild=guild.id)

    # ------------------------------------------------------------------
    # Voice-output gate for AgentMessageHandler
    # ------------------------------------------------------------------

    async def speak_if_in_channel(
        self, message: discord.Message, text: str,
    ) -> bool:
        """Speak ``text`` via TTS iff the message author is in Poob's VC.

        Called by AgentMessageHandler after it posts a text reply so that
        chat @mentions from users inside the VC also hear the response,
        while chat @mentions from outside the VC get text only. No opt-in
        toggle — the VC-membership check is the deterministic gate.

        Args:
            message: The triggering text message.
            text: The response text to synthesize.

        Returns:
            True if audio was played, False otherwise.
        """
        # DMs and non-guild messages can never share a VC with the bot.
        if not message.guild:
            return False

        session = self._get_session(message.guild.id)
        if session is None:
            return False

        # Must have voice state with a channel matching the session's VC.
        author_voice = getattr(message.author, "voice", None)
        if author_voice is None or author_voice.channel is None:
            return False
        if author_voice.channel != session.voice_client.channel:
            return False

        if not text or not text.strip():
            return False

        try:
            audio = await session._synthesize(text)
            if not audio:
                return False
            await session._play_audio(audio)
            log.info(
                "Spoke chat response in VC",
                user=message.author.id,
                bytes=len(audio),
            )
            return True
        except Exception as exc:
            log.warning("speak_if_in_channel failed", error=str(exc)[:100])
            return False

    # ------------------------------------------------------------------
    # Voice state bookkeeping
    # ------------------------------------------------------------------

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
