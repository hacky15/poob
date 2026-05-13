"""Tests for the now-playing UI extensions — previous, replay, leave buttons
plus the active-effect indicator in the embed.

The existing test_music_ui.py covers the original 7 buttons + embed rendering.
This file focuses on the three new buttons and the embed indicator added
for feature #3 of the music-bot roadmap. See
docs/decisions/music-now-playing-embed-buttons.md.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from poob.discord_bot.music_ui import (
    MusicControlsView,
    build_now_playing_embed,
)
from poob.music.queue import Track


def _t(name: str, *, duration_s: int = 180) -> Track:
    return Track(
        title=name,
        url=f"https://youtube.com/watch?v={name}",
        duration=timedelta(seconds=duration_s),
        requester_name="Tester",
        thumbnail=f"https://example.com/{name}.jpg",
    )


def _make_interaction(*, guild_id: int = 10, user_id: int = 1) -> MagicMock:
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


# ---------------------------------------------------------------------------
# New buttons exist with the right custom_id
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_view_registers_previous_button_with_stable_custom_id() -> None:
    """Persistent views require stable ``custom_id`` strings; the registered
    handler in ``bot.add_view`` is keyed by them. Don't ever rename these
    without also clearing the registered view."""
    view = MusicControlsView()
    custom_ids = {item.custom_id for item in view.children if hasattr(item, "custom_id")}
    assert "poob:music:previous" in custom_ids


@pytest.mark.asyncio
async def test_view_registers_replay_button() -> None:
    view = MusicControlsView()
    custom_ids = {item.custom_id for item in view.children if hasattr(item, "custom_id")}
    assert "poob:music:replay" in custom_ids


@pytest.mark.asyncio
async def test_view_registers_leave_button() -> None:
    view = MusicControlsView()
    custom_ids = {item.custom_id for item in view.children if hasattr(item, "custom_id")}
    assert "poob:music:leave" in custom_ids


@pytest.mark.asyncio
async def test_existing_buttons_still_present() -> None:
    """Regression guard — adding new buttons must not silently lose
    existing ones (the persistent-view re-registration would orphan
    them on prior posted embeds)."""
    view = MusicControlsView()
    custom_ids = {item.custom_id for item in view.children if hasattr(item, "custom_id")}
    for old in (
        "poob:music:pause", "poob:music:resume", "poob:music:skip",
        "poob:music:stop", "poob:music:shuffle", "poob:music:loop",
        "poob:music:queue",
    ):
        assert old in custom_ids


# ---------------------------------------------------------------------------
# Button dispatch — verify each new button forwards the right action
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_previous_button_dispatches_previous_action() -> None:
    handler = AsyncMock(return_value="[SILENT]Back to Old Song.")
    view = MusicControlsView(handler)
    btn = next(
        item for item in view.children
        if getattr(item, "custom_id", None) == "poob:music:previous"
    )
    interaction = _make_interaction()

    await btn.callback(interaction)

    handler.assert_awaited_once()
    kwargs = handler.await_args.kwargs
    assert kwargs["tool_args"]["action"] == "previous"
    assert kwargs["user_id"] == 1
    assert kwargs["guild_id"] == 10


@pytest.mark.asyncio
async def test_replay_button_dispatches_replay_action() -> None:
    handler = AsyncMock(return_value="[SILENT]Restarting song from the top.")
    view = MusicControlsView(handler)
    btn = next(
        item for item in view.children
        if getattr(item, "custom_id", None) == "poob:music:replay"
    )
    interaction = _make_interaction()

    await btn.callback(interaction)

    assert handler.await_args.kwargs["tool_args"]["action"] == "replay"


@pytest.mark.asyncio
async def test_leave_button_dispatches_leave_action() -> None:
    handler = AsyncMock(return_value="[SILENT]Left voice channel.")
    view = MusicControlsView(handler)
    btn = next(
        item for item in view.children
        if getattr(item, "custom_id", None) == "poob:music:leave"
    )
    interaction = _make_interaction()

    await btn.callback(interaction)

    assert handler.await_args.kwargs["tool_args"]["action"] == "leave"


@pytest.mark.asyncio
async def test_new_buttons_strip_silent_prefix_in_ephemeral_reply() -> None:
    """Same contract the other buttons follow — the ``[SILENT]`` marker is
    internal; the user sees clean text."""
    handler = AsyncMock(return_value="[SILENT]Back to Old Song.")
    view = MusicControlsView(handler)
    btn = next(
        item for item in view.children
        if getattr(item, "custom_id", None) == "poob:music:previous"
    )
    interaction = _make_interaction()

    await btn.callback(interaction)

    interaction.followup.send.assert_awaited_once()
    msg = interaction.followup.send.await_args.args[0]
    assert "[SILENT]" not in msg
    assert "Back to Old Song" in msg


# ---------------------------------------------------------------------------
# Embed — active-effect indicator
# ---------------------------------------------------------------------------

def test_embed_does_not_show_effect_field_when_none() -> None:
    """Default state — no effect, no field. Keeps the embed tidy."""
    track = _t("song")
    embed = build_now_playing_embed(track, queue_size=0, active_effect="none")

    field_names = {f.name for f in embed.fields}
    assert "Effect" not in field_names


def test_embed_shows_effect_field_when_active() -> None:
    track = _t("song")
    embed = build_now_playing_embed(track, queue_size=0, active_effect="nightcore")

    field_names = {f.name for f in embed.fields}
    assert "Effect" in field_names
    effect_field = next(f for f in embed.fields if f.name == "Effect")
    assert "nightcore" in effect_field.value.lower()


def test_embed_active_effect_defaults_to_none() -> None:
    """Backwards compatibility — old call sites that don't pass the new
    kwarg must still render a sensible embed."""
    track = _t("song")
    embed = build_now_playing_embed(track, queue_size=0)  # no active_effect kwarg

    field_names = {f.name for f in embed.fields}
    assert "Effect" not in field_names
