"""Music UI — now-playing embed and persistent control buttons.

Every user-facing music control funnels through the same
``MusicCog.handle_music_request(..., tool_args={...})`` contract the voice
and text @mention paths use. Button clicks do not bypass Poob — they produce
structured ``tool_args`` the same way the LLM does, and the handler updates
the brain's ``_music_playing_info`` context as a side effect, keeping state
in sync across all input modalities.

Embed design follows the post-Rythm convention: hyperlinked title, uploader,
thumbnail (top-right, from yt-dlp's chosen URL), duration, footer with
requester avatar. No live progress bar — Discord's 5-edit/5-second per-channel
rate limit makes that counterproductive.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Awaitable, Callable

import discord

from poob.utils.logging import get_logger

if TYPE_CHECKING:
    from poob.music.player import GuildMusicPlayer
    from poob.music.queue import Track

log = get_logger("discord.music_ui")

# Poob's brand colour for all music embeds. Single colour, not per-song —
# matches the convention used by Hydra/Jockie/Beatra.
_EMBED_COLOR = 0x8B4FBE  # muted purple

# Callback signature matches MusicCog.handle_music_request.
# (request: str, user_id: int, guild_id: int, *, voice: bool, tool_args: dict) -> str
MusicHandler = Callable[..., Awaitable[str]]


def build_now_playing_embed(
    track: "Track",
    queue_size: int = 0,
    *,
    active_effect: str = "none",
) -> discord.Embed:
    """Render a Track into the standard now-playing embed.

    Args:
        track: The currently playing track.
        queue_size: Number of tracks waiting behind this one.
        active_effect: Current audio-effect preset name. ``"none"``
            (default) means no effect; the field is omitted in that
            case to keep the embed tidy. Anything else surfaces an
            "Effect: …" field so users can see at a glance that
            nightcore / slowed / etc. is on.

    Returns:
        A Discord embed ready to send.
    """
    embed = discord.Embed(
        title=track.title,
        url=track.url,
        color=_EMBED_COLOR,
    )

    # Top-right thumbnail — yt-dlp picks the best available URL; don't
    # hardcode maxresdefault (404s on Shorts, livestreams, old uploads).
    if track.thumbnail:
        embed.set_thumbnail(url=track.thumbnail)

    embed.add_field(name="Duration", value=track.duration_str, inline=True)

    if queue_size > 0:
        embed.add_field(
            name="Up Next",
            value=f"{queue_size} track{'s' if queue_size != 1 else ''} queued",
            inline=True,
        )

    if active_effect and active_effect != "none":
        embed.add_field(
            name="Effect",
            value=active_effect.replace("_", " "),
            inline=True,
        )

    if track.requester_name:
        embed.set_footer(text=f"Requested by {track.requester_name}")

    return embed


class MusicControlsView(discord.ui.View):
    """Persistent control bar attached to the now-playing embed.

    ``timeout=None`` + stable ``custom_id`` values let the view survive bot
    restarts; re-register once via ``bot.add_view(MusicControlsView(...))``
    in ``on_ready``.

    Every button delegates to the same handler the voice/text LLM paths use,
    passing structured ``tool_args``. The LLM is deliberately skipped — the
    button's intent is already classified.
    """

    def __init__(self, music_handler: MusicHandler | None = None) -> None:
        super().__init__(timeout=None)
        self._handler = music_handler

    def set_handler(self, handler: MusicHandler) -> None:
        """Wire the handler after construction (for persistent view restore)."""
        self._handler = handler

    async def _dispatch(
        self,
        interaction: discord.Interaction,
        action: str,
        value: int | None = None,
    ) -> None:
        """Invoke the music handler with structured tool_args."""
        if self._handler is None:
            await interaction.response.send_message(
                "Music system not ready.", ephemeral=True,
            )
            return

        # Guard: must be in a guild + Poob must be in a VC we can target.
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "These controls only work in a server.", ephemeral=True,
            )
            return

        # Ack quickly so Discord doesn't time out the interaction (3s window).
        await interaction.response.defer(ephemeral=True, invisible=False)

        tool_args: dict = {"action": action}
        if value is not None:
            tool_args["value"] = value

        try:
            result = await self._handler(
                request="",
                user_id=interaction.user.id,
                guild_id=guild.id,
                voice=False,
                tool_args=tool_args,
            )
        except Exception as exc:
            log.warning("Music button dispatch failed",
                        action=action, error=str(exc)[:100])
            await interaction.followup.send(
                "Something broke.", ephemeral=True,
            )
            return

        # [SILENT] prefix = control command. Show the status ephemerally so
        # only the clicker sees it — no channel spam.
        status = result[8:].strip() if result.startswith("[SILENT]") else result.strip()
        if not status:
            status = f"{action.capitalize()} done."
        await interaction.followup.send(status, ephemeral=True)

    @discord.ui.button(
        emoji="⏸",
        style=discord.ButtonStyle.secondary,
        custom_id="poob:music:pause",
        row=0,
    )
    async def pause_btn(
        self, button: discord.ui.Button, interaction: discord.Interaction,
    ) -> None:
        await self._dispatch(interaction, "pause")

    @discord.ui.button(
        emoji="▶️",
        style=discord.ButtonStyle.secondary,
        custom_id="poob:music:resume",
        row=0,
    )
    async def resume_btn(
        self, button: discord.ui.Button, interaction: discord.Interaction,
    ) -> None:
        await self._dispatch(interaction, "resume")

    @discord.ui.button(
        emoji="⏭",
        style=discord.ButtonStyle.primary,
        custom_id="poob:music:skip",
        row=0,
    )
    async def skip_btn(
        self, button: discord.ui.Button, interaction: discord.Interaction,
    ) -> None:
        await self._dispatch(interaction, "skip")

    @discord.ui.button(
        emoji="⏹",
        style=discord.ButtonStyle.danger,
        custom_id="poob:music:stop",
        row=0,
    )
    async def stop_btn(
        self, button: discord.ui.Button, interaction: discord.Interaction,
    ) -> None:
        await self._dispatch(interaction, "stop")

    @discord.ui.button(
        emoji="🔀",
        style=discord.ButtonStyle.secondary,
        custom_id="poob:music:shuffle",
        row=1,
    )
    async def shuffle_btn(
        self, button: discord.ui.Button, interaction: discord.Interaction,
    ) -> None:
        await self._dispatch(interaction, "shuffle")

    @discord.ui.button(
        emoji="🔁",
        style=discord.ButtonStyle.secondary,
        custom_id="poob:music:loop",
        row=1,
    )
    async def loop_btn(
        self, button: discord.ui.Button, interaction: discord.Interaction,
    ) -> None:
        await self._dispatch(interaction, "loop")

    @discord.ui.button(
        emoji="📜",
        label="Queue",
        style=discord.ButtonStyle.secondary,
        custom_id="poob:music:queue",
        row=1,
    )
    async def queue_btn(
        self, button: discord.ui.Button, interaction: discord.Interaction,
    ) -> None:
        await self._dispatch(interaction, "queue")

    # ---- Row 2: history / track-restart / disconnect ----
    # These map 1:1 to the music_assistant actions added in
    # decisions/music-queue-primitives.md. The leave button reaches
    # cross-cog (MusicCog handler -> VoiceCog._force_disconnect); see
    # decisions/music-now-playing-embed-buttons.md.

    @discord.ui.button(
        emoji="⏮",
        style=discord.ButtonStyle.secondary,
        custom_id="poob:music:previous",
        row=2,
    )
    async def previous_btn(
        self, button: discord.ui.Button, interaction: discord.Interaction,
    ) -> None:
        await self._dispatch(interaction, "previous")

    @discord.ui.button(
        emoji="🔄",
        style=discord.ButtonStyle.secondary,
        custom_id="poob:music:replay",
        row=2,
    )
    async def replay_btn(
        self, button: discord.ui.Button, interaction: discord.Interaction,
    ) -> None:
        await self._dispatch(interaction, "replay")

    @discord.ui.button(
        emoji="🚪",
        label="Leave",
        style=discord.ButtonStyle.danger,
        custom_id="poob:music:leave",
        row=2,
    )
    async def leave_btn(
        self, button: discord.ui.Button, interaction: discord.Interaction,
    ) -> None:
        await self._dispatch(interaction, "leave")


def build_now_playing_message(
    player: "GuildMusicPlayer",
    music_handler: MusicHandler,
) -> tuple[discord.Embed, MusicControlsView] | None:
    """Build the complete (embed, view) pair for the current track.

    Returns None if nothing is currently playing.
    """
    track = player.current_track
    if track is None:
        return None
    embed = build_now_playing_embed(
        track,
        queue_size=player.queue.size,
        active_effect=player.active_effect,
    )
    view = MusicControlsView(music_handler)
    return embed, view
