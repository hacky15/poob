"""Multi-guild isolation tests for PoobBrain.

Covers the invariant from
docs/plans/poobbrain-multi-guild-isolation.md: every mutable state
container that depends on user identity is keyed (guild_id, user_id),
and every guild-specific scratchpad is keyed by guild_id alone. There
are zero singleton mutable fields on PoobBrain that cross guilds.
"""

from __future__ import annotations

import asyncio

import pytest

from poob.brain.poob import PoobBrain


def _make_brain() -> PoobBrain:
    """PoobBrain with no real LLM keys — we only exercise its state."""
    return PoobBrain(groq_api_key="", deal_agent=None)


# ---------------------------------------------------------------------------
# 1. History isolation per (guild, user)
# ---------------------------------------------------------------------------

def test_history_isolated_per_guild_for_same_user() -> None:
    brain = _make_brain()
    user = "user-123"

    brain._save_response(10, user, "msg from guild 10")
    brain._save_response(20, user, "msg from guild 20")

    h10 = brain._histories[(10, user)]
    h20 = brain._histories[(20, user)]

    assert len(h10) == 1
    assert len(h20) == 1
    assert h10[0].content == "msg from guild 10"
    assert h20[0].content == "msg from guild 20"
    # The dicts must NOT share a list — mutating one must not bleed
    h10.append(brain._histories[(10, user)][0].__class__(role="assistant", content="X"))
    assert len(brain._histories[(20, user)]) == 1


def test_build_messages_returns_disjoint_history_per_guild() -> None:
    brain = _make_brain()
    user = "u"

    msgs10 = brain._build_messages(user, "hello in 10", guild_id=10)
    msgs20 = brain._build_messages(user, "hello in 20", guild_id=20)

    user_lines_10 = [m for m in msgs10 if m["role"] == "user"]
    user_lines_20 = [m for m in msgs20 if m["role"] == "user"]
    # Each guild sees only its own user message in the conversation.
    assert any("hello in 10" in m["content"] for m in user_lines_10)
    assert not any("hello in 20" in m["content"] for m in user_lines_10)
    assert any("hello in 20" in m["content"] for m in user_lines_20)
    assert not any("hello in 10" in m["content"] for m in user_lines_20)


def test_dm_isolated_from_guild_for_same_user() -> None:
    brain = _make_brain()
    user = "u"
    brain._save_response(0, user, "DM only")
    brain._save_response(10, user, "guild only")
    assert len(brain._histories[(0, user)]) == 1
    assert len(brain._histories[(10, user)]) == 1
    assert brain._histories[(0, user)][0].content == "DM only"
    assert brain._histories[(10, user)][0].content == "guild only"


# ---------------------------------------------------------------------------
# 2. Deal-context isolation
# ---------------------------------------------------------------------------

def test_deal_context_isolated_per_guild() -> None:
    brain = _make_brain()
    user = "u"

    brain._update_deal_context(10, user, "Are you sure about that?")
    brain._update_deal_context(20, user, "")  # no question — clears guild 20

    assert brain._deal_context.get((10, user)) is not None
    assert brain._deal_context.get((20, user)) is None


# ---------------------------------------------------------------------------
# 3. Dedup is per-guild
# ---------------------------------------------------------------------------

def test_is_duplicate_play_per_guild() -> None:
    brain = _make_brain()
    user = "u"
    q = "Bohemian Rhapsody"

    # First play in guild 10 — not a duplicate
    assert brain._is_duplicate_play(10, user, q) is False
    # Same query, different guild — also not a duplicate
    assert brain._is_duplicate_play(20, user, q) is False
    # Repeat in guild 10 within the window — IS a duplicate
    assert brain._is_duplicate_play(10, user, q) is True
    # Repeat in guild 20 within the window — also duplicate (its own record)
    assert brain._is_duplicate_play(20, user, q) is True


def test_clear_play_on_failure_only_clears_matching_guild() -> None:
    brain = _make_brain()
    user = "u"
    q = "Some Song"
    brain._is_duplicate_play(10, user, q)  # records guild 10
    brain._is_duplicate_play(20, user, q)  # records guild 20
    brain._clear_play_on_failure(10, user, q)
    # After clearing 10, 10 is gone, 20 remains.
    assert (10, user) not in brain._last_play
    assert (20, user) in brain._last_play


# ---------------------------------------------------------------------------
# 4. Horniness is per-guild
# ---------------------------------------------------------------------------

def test_horniness_default_per_guild() -> None:
    brain = _make_brain()
    assert brain._horniness_for(10) == 5  # default
    assert brain._horniness_for(20) == 5  # default


def test_roll_horniness_only_affects_one_guild() -> None:
    brain = _make_brain()
    rolled = brain.roll_horniness(10)
    assert 1 <= rolled <= 10
    assert brain._horniness_for(10) == rolled
    assert brain._horniness_for(20) == 5  # unchanged


# ---------------------------------------------------------------------------
# 5. Music-playing info is per-guild
# ---------------------------------------------------------------------------

def test_music_playing_info_per_guild() -> None:
    brain = _make_brain()
    brain._set_music_playing_info(10, "Track A [3:00]")
    brain._set_music_playing_info(20, "Track B [4:00]")
    assert brain._get_music_playing_info(10) == "Track A [3:00]"
    assert brain._get_music_playing_info(20) == "Track B [4:00]"
    # Clearing one doesn't affect the other.
    brain._set_music_playing_info(10, "")
    assert brain._get_music_playing_info(10) == ""
    assert brain._get_music_playing_info(20) == "Track B [4:00]"


def test_build_messages_inserts_only_this_guilds_music_info() -> None:
    brain = _make_brain()
    brain._set_music_playing_info(10, "Track A [3:00]")
    brain._set_music_playing_info(20, "Track B [4:00]")

    msgs10 = brain._build_messages("u", "hello", voice=True, guild_id=10)
    msgs20 = brain._build_messages("u", "hello", voice=True, guild_id=20)
    sys10 = next(m for m in msgs10 if m["role"] == "system")
    sys20 = next(m for m in msgs20 if m["role"] == "system")

    assert "Track A" in sys10["content"]
    assert "Track B" not in sys10["content"]
    assert "Track B" in sys20["content"]
    assert "Track A" not in sys20["content"]


# ---------------------------------------------------------------------------
# 6. Concurrent dispatch — no shared mutable guild_id field
# ---------------------------------------------------------------------------

def test_no_shared_voice_guild_id_field() -> None:
    """Regression guard: the old `_voice_guild_id` singleton must not
    return. Concurrent voice sessions in different guilds depend on
    guild_id arriving via method args, not via mutable shared state.
    """
    brain = _make_brain()
    assert not hasattr(brain, "_voice_guild_id"), (
        "_voice_guild_id was a multi-guild race risk; do not reintroduce it"
    )


def test_no_shared_horniness_level_field() -> None:
    """Same regression guard for the old singleton horniness."""
    brain = _make_brain()
    # The dataclass field that exists is _horniness_levels (dict).
    # _horniness_level (singular int) must not exist.
    assert not hasattr(brain, "_horniness_level"), (
        "_horniness_level singleton was a multi-guild race risk"
    )


# ---------------------------------------------------------------------------
# 7. clear_history semantics
# ---------------------------------------------------------------------------

def test_clear_history_for_user_clears_all_guilds() -> None:
    brain = _make_brain()
    brain._save_response(10, "u", "in 10")
    brain._save_response(20, "u", "in 20")
    brain._save_response(0, "u", "in DM")
    brain._save_response(10, "other", "other-user")

    brain.clear_history("u")  # no guild_id — clears all of "u"

    assert (10, "u") not in brain._histories
    assert (20, "u") not in brain._histories
    assert (0, "u") not in brain._histories
    # Other user untouched
    assert (10, "other") in brain._histories


def test_clear_history_for_user_in_one_guild_only() -> None:
    brain = _make_brain()
    brain._save_response(10, "u", "in 10")
    brain._save_response(20, "u", "in 20")

    brain.clear_history("u", guild_id=10)

    assert (10, "u") not in brain._histories
    assert (20, "u") in brain._histories


# ---------------------------------------------------------------------------
# 8. Concurrent respond_streaming sees its own guild
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_concurrent_respond_streaming_does_not_cross_contaminate() -> None:
    """Two simultaneous respond_streaming calls with different guild_ids
    must not cross-pollute. We can't easily exercise the LLM path
    without keys, but we CAN verify _build_messages is reentrant and
    each call sees its own guild's state when invoked concurrently.
    """
    brain = _make_brain()
    brain._set_music_playing_info(10, "Track A [3:00]")
    brain._set_music_playing_info(20, "Track B [4:00]")

    async def build(g: int, label: str) -> list[dict]:
        # Yield to the loop so we definitely interleave with the other call.
        await asyncio.sleep(0)
        msgs = brain._build_messages("u", label, voice=True, guild_id=g)
        await asyncio.sleep(0)
        return msgs

    msgs10, msgs20 = await asyncio.gather(
        build(10, "from-10"), build(20, "from-20"),
    )
    sys10 = next(m for m in msgs10 if m["role"] == "system")
    sys20 = next(m for m in msgs20 if m["role"] == "system")
    assert "Track A" in sys10["content"]
    assert "Track B" not in sys10["content"]
    assert "Track B" in sys20["content"]
    assert "Track A" not in sys20["content"]
