"""Tests for effect-OFF routing in PoobBrain.

Root cause: Music routing model maps "slow"/"nightcore" → apply_effect ON,
but "remove X"/"turn off X" don't route to apply_effect(effect='none').
They fall to casual path, which fabricates success but never calls apply_effect.

Fix: Route effect-removal phrasings ("remove X", "turn off X", "normal",
"stop slowing", etc.) to music_assistant(action='apply_effect', effect='none').
Block casual path from answering music/effect state changes.

See docs/decisions/effect-removal-routing.md for design rationale.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from poob.brain.poob import PoobBrain


def _make_brain(
    *,
    deal_agent: Any | None = None,
) -> PoobBrain:
    """PoobBrain with a fake Groq key so routing branches run."""
    return PoobBrain(
        groq_api_key="test-key",
        deal_agent=deal_agent,
    )


# ---------------------------------------------------------------------------
# Effect-OFF routing: removal phrasings must route to apply_effect(none)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_remove_effect_routes_to_music_assistant() -> None:
    """'Remove nightcore' → apply_effect(effect='none')."""
    brain = _make_brain(deal_agent=AsyncMock())
    brain._music_handler = AsyncMock(return_value="[SILENT]Effect cleared.")

    with patch.object(
        brain,
        "_groq_with_tools",
        new=AsyncMock(
            return_value=(
                "",
                "music_assistant",
                {
                    "action": "apply_effect",
                    "effect": "none",
                },
            )
        ),
    ):
        out = await brain.respond(
            "remove nightcore",
            user_id="123",
            guild_id=10,
        )

    brain._music_handler.assert_called_once()
    call_kwargs = brain._music_handler.call_args[1]
    assert call_kwargs["tool_args"]["action"] == "apply_effect"
    assert call_kwargs["tool_args"]["effect"] == "none"
    assert "cleared" in out.lower() or "effect" in out.lower()


@pytest.mark.asyncio
async def test_turn_off_effect_routes_to_apply_effect() -> None:
    """'Turn off the effect' → apply_effect(effect='none')."""
    brain = _make_brain(deal_agent=AsyncMock())
    brain._music_handler = AsyncMock(return_value="[SILENT]Audio effect cleared.")

    with patch.object(
        brain,
        "_groq_with_tools",
        new=AsyncMock(
            return_value=(
                "",
                "music_assistant",
                {
                    "action": "apply_effect",
                    "effect": "none",
                },
            )
        ),
    ):
        out = await brain.respond(
            "turn off the effect",
            user_id="123",
            guild_id=10,
        )

    brain._music_handler.assert_called_once()
    call_kwargs = brain._music_handler.call_args[1]
    assert call_kwargs["tool_args"]["effect"] == "none"


@pytest.mark.asyncio
async def test_stop_slowing_routes_to_apply_effect_none() -> None:
    """'Stop slowing it' → apply_effect(effect='none')."""
    brain = _make_brain(deal_agent=AsyncMock())
    brain._music_handler = AsyncMock(return_value="[SILENT]Slowed effect cleared.")

    with patch.object(
        brain,
        "_groq_with_tools",
        new=AsyncMock(
            return_value=(
                "",
                "music_assistant",
                {
                    "action": "apply_effect",
                    "effect": "none",
                },
            )
        ),
    ):
        out = await brain.respond(
            "stop slowing it",
            user_id="123",
            guild_id=10,
        )

    brain._music_handler.assert_called_once()
    call_kwargs = brain._music_handler.call_args[1]
    assert call_kwargs["tool_args"]["effect"] == "none"


@pytest.mark.asyncio
async def test_normal_playback_routes_to_apply_effect_none() -> None:
    """'Normal playback' / 'normal speed' → apply_effect(effect='none')."""
    brain = _make_brain(deal_agent=AsyncMock())
    brain._music_handler = AsyncMock(return_value="[SILENT]Removed effect.")

    with patch.object(
        brain,
        "_groq_with_tools",
        new=AsyncMock(
            return_value=(
                "",
                "music_assistant",
                {
                    "action": "apply_effect",
                    "effect": "none",
                },
            )
        ),
    ):
        out = await brain.respond(
            "normal playback",
            user_id="123",
            guild_id=10,
        )

    brain._music_handler.assert_called_once()
    call_kwargs = brain._music_handler.call_args[1]
    assert call_kwargs["tool_args"]["effect"] == "none"


@pytest.mark.asyncio
async def test_no_effect_routes_to_apply_effect_none() -> None:
    """'No effect' → apply_effect(effect='none')."""
    brain = _make_brain(deal_agent=AsyncMock())
    brain._music_handler = AsyncMock(return_value="[SILENT]Effect disabled.")

    with patch.object(
        brain,
        "_groq_with_tools",
        new=AsyncMock(
            return_value=(
                "",
                "music_assistant",
                {
                    "action": "apply_effect",
                    "effect": "none",
                },
            )
        ),
    ):
        out = await brain.respond(
            "no effect",
            user_id="123",
            guild_id=10,
        )

    brain._music_handler.assert_called_once()
    call_kwargs = brain._music_handler.call_args[1]
    assert call_kwargs["tool_args"]["effect"] == "none"


@pytest.mark.asyncio
async def test_clear_effect_routes_to_apply_effect_none() -> None:
    """'Clear the effect' → apply_effect(effect='none')."""
    brain = _make_brain(deal_agent=AsyncMock())
    brain._music_handler = AsyncMock(return_value="[SILENT]Effect cleared.")

    with patch.object(
        brain,
        "_groq_with_tools",
        new=AsyncMock(
            return_value=(
                "",
                "music_assistant",
                {
                    "action": "apply_effect",
                    "effect": "none",
                },
            )
        ),
    ):
        out = await brain.respond(
            "clear the effect",
            user_id="123",
            guild_id=10,
        )

    brain._music_handler.assert_called_once()
    call_kwargs = brain._music_handler.call_args[1]
    assert call_kwargs["tool_args"]["effect"] == "none"


# ---------------------------------------------------------------------------
# Effect-ON still routes correctly (regression)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_effect_on_still_routes() -> None:
    """'Make it nightcore' → apply_effect(effect='nightcore'). Regression guard."""
    brain = _make_brain(deal_agent=AsyncMock())
    brain._music_handler = AsyncMock(return_value="[SILENT]Applied nightcore.")

    with patch.object(
        brain,
        "_groq_with_tools",
        new=AsyncMock(
            return_value=(
                "",
                "music_assistant",
                {
                    "action": "apply_effect",
                    "effect": "nightcore",
                },
            )
        ),
    ):
        out = await brain.respond(
            "make it nightcore",
            user_id="123",
            guild_id=10,
        )

    brain._music_handler.assert_called_once()
    call_kwargs = brain._music_handler.call_args[1]
    assert call_kwargs["tool_args"]["effect"] == "nightcore"


@pytest.mark.asyncio
async def test_slow_it_down_routes_apply_effect_slowed() -> None:
    """'Slow it down' → apply_effect(effect='slowed'). Regression guard."""
    brain = _make_brain(deal_agent=AsyncMock())
    brain._music_handler = AsyncMock(return_value="[SILENT]Applied slowed.")

    with patch.object(
        brain,
        "_groq_with_tools",
        new=AsyncMock(
            return_value=(
                "",
                "music_assistant",
                {
                    "action": "apply_effect",
                    "effect": "slowed",
                },
            )
        ),
    ):
        out = await brain.respond(
            "slow it down",
            user_id="123",
            guild_id=10,
        )

    brain._music_handler.assert_called_once()
    call_kwargs = brain._music_handler.call_args[1]
    assert call_kwargs["tool_args"]["effect"] == "slowed"


# ---------------------------------------------------------------------------
# Block casual path from claiming music state changes it never performed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_casual_path_cannot_claim_effect_applied() -> None:
    """The casual path must never claim an effect it did not apply.

    **2026-07-27: this test now pins the FIX rather than the bug.** It used to
    simulate the router returning no tool for "remove the nightcore" and
    assert ``_music_handler.assert_not_called()`` — i.e. it characterised the
    defect its own docstring described ("casual path returns something
    plausible but false"), and merely hoped the routing prompt would prevent
    it.

    The deterministic effect-clear override
    (docs/incidents/effect-clear-suppressed-by-normal-guard.md) closes that
    hole at the layer that can actually guarantee it: even with the router
    returning nothing, "remove the nightcore" is forced to
    apply_effect/effect=none, the music handler really runs, and the casual
    fallback is never reached — so there is nothing left to lie with.
    """
    brain = _make_brain(deal_agent=AsyncMock())
    brain._music_handler = AsyncMock(return_value="[SILENT]Applied.")

    with (
        patch.object(
            brain,
            "_groq_with_tools",
            new=AsyncMock(return_value=("Effect applied!", None, None)),
        ),
        patch.object(
            brain,
            "_casual_text_fallback",
            new=AsyncMock(return_value="Effect is now removed."),
        ) as casual_fb,
    ):
        out = await brain.respond(
            "remove the nightcore",
            user_id="123",
            guild_id=10,
        )

    # The effect is genuinely applied, by the deterministic override.
    brain._music_handler.assert_called_once()
    assert brain._music_handler.call_args.kwargs["tool_args"] == {
        "action": "apply_effect",
        "effect": "nightcore",
        "mode": "remove",
    }
    # And the casual path — the only thing that could fabricate success —
    # never ran at all.
    casual_fb.assert_not_called()
    assert "Applied" in out


# ---------------------------------------------------------------------------
# System prompt must route effect-removal explicitly
# ---------------------------------------------------------------------------


def test_music_prompt_routes_effect_removal() -> None:
    """The tool block must contain explicit guidance on routing effect-removal
    phrasings to music_assistant(action='apply_effect', effect='none').
    Guards against the root cause: no routing rule = casual-path fallthrough."""
    from poob.brain.poob import _build_system_prompt

    p = _build_system_prompt(level=5, voice=False, with_tools=True).lower()

    # Must mention effect removal + the 'none' value as the way to turn off.
    assert "effect" in p, "missing 'effect' in music-routing guidance"
    assert "turn off" in p or "remove" in p or "clear" in p, (
        "missing explicit routing for effect removal (turn off/remove/clear)"
    )
    assert "none" in p, "missing 'none' as the value for clearing effects"


def test_music_tool_definition_routes_effect_none() -> None:
    """MUSIC_TOOL.effect parameter must document that 'none' clears effects."""
    from poob.brain.poob import MUSIC_TOOL

    effect_desc = MUSIC_TOOL["function"]["parameters"]["properties"]["effect"][
        "description"
    ].lower()
    assert "none" in effect_desc, "effect desc missing 'none' for clearing"
    assert "clear" in effect_desc or "remove" in effect_desc, (
        "effect desc missing clear/remove language"
    )


def test_music_prompt_phrasings_for_effect_removal() -> None:
    """The prompt must teach specific phrasings for effect removal:
    'turn off', 'remove', 'clear', 'stop', 'normal', etc.
    This ensures the LLM recognizes these as apply_effect(none) candidates."""
    from poob.brain.poob import _build_system_prompt

    p = _build_system_prompt(level=5, voice=False, with_tools=True).lower()

    # At least some removal phrasings should be explicitly listed as examples.
    removal_phrasings = [
        "turn off",
        "remove",
        "clear",
        "stop",
        "normal",
    ]
    found = sum(1 for phrase in removal_phrasings if phrase in p)
    assert found >= 2, (
        f"Prompt must list at least 2 removal phrasings; found {found}. "
        "Examples: 'turn off', 'remove', 'clear', 'stop', 'normal'."
    )


# ---------------------------------------------------------------------------
# Multi-guild isolation still holds for effect routing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_effect_removal_respects_guild_isolation() -> None:
    """Effect removal in guild A must not affect guild B's player."""
    brain = _make_brain(deal_agent=AsyncMock())

    # Two separate music handlers (one per guild).
    music_handler_a = AsyncMock(return_value="[SILENT]Cleared A.")
    music_handler_b = AsyncMock(return_value="[SILENT]Cleared B.")

    # Both use the same brain instance.
    brain._music_handler = music_handler_a

    with patch.object(
        brain,
        "_groq_with_tools",
        new=AsyncMock(
            return_value=(
                "",
                "music_assistant",
                {
                    "action": "apply_effect",
                    "effect": "none",
                },
            )
        ),
    ):
        await brain.respond("remove nightcore", user_id="123", guild_id=10)

    # Handler was called with guild_id=10 (as positional arg: original_message, user_id, guild_id).
    assert music_handler_a.call_args[0][2] == 10

    # Swap handler for another guild.
    brain._music_handler = music_handler_b

    with patch.object(
        brain,
        "_groq_with_tools",
        new=AsyncMock(
            return_value=(
                "",
                "music_assistant",
                {
                    "action": "apply_effect",
                    "effect": "none",
                },
            )
        ),
    ):
        await brain.respond("remove nightcore", user_id="123", guild_id=20)

    # Handler was called with guild_id=20.
    assert music_handler_b.call_args[0][2] == 20
