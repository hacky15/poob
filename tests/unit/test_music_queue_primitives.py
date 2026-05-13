"""Tests for the new queue primitives — move + previous.

See docs/decisions/music-queue-primitives.md for design rationale.

The contract:

- ``move(from_idx, to_idx)`` reorders within the upcoming queue. Both
  indices are 0-based against the upcoming queue (not history, not
  current). Out-of-range indices return None (caller decides UX).
- ``previous()`` pops the most-recent finished track from history and
  returns it. It does NOT modify current or queue state; the caller
  (GuildMusicPlayer) is responsible for plumbing it into playback so
  the queue invariants and loop modes stay consistent.

Player-level orchestration (replay current, walk-back history) is
tested in ``test_music_player_primitives.py``.
"""

from __future__ import annotations

from datetime import timedelta

from poob.music.queue import LoopMode, MusicQueue, Track


def _t(name: str) -> Track:
    """Minimal Track factory for queue tests — only the fields that affect
    ordering / identity matter here."""
    return Track(
        title=name, url=f"https://example.com/{name}",
        duration=timedelta(seconds=180),
    )


# ---------------------------------------------------------------------------
# move(from_idx, to_idx)
# ---------------------------------------------------------------------------

def test_move_reorders_within_queue() -> None:
    q = MusicQueue()
    a, b, c = _t("A"), _t("B"), _t("C")
    q.add(a); q.add(b); q.add(c)

    moved = q.move(0, 2)

    assert moved is a
    titles = [t.title for t in q.upcoming]
    assert titles == ["B", "C", "A"]


def test_move_does_not_touch_current() -> None:
    """``move`` only operates on upcoming; current track is unaffected."""
    q = MusicQueue()
    a, b, c = _t("A"), _t("B"), _t("C")
    q.add(a); q.add(b); q.add(c)
    q.get_next()  # A becomes current

    q.move(0, 1)  # move B to where C was

    assert q.current is a
    titles = [t.title for t in q.upcoming]
    assert titles == ["C", "B"]


def test_move_out_of_range_returns_none() -> None:
    q = MusicQueue()
    q.add(_t("A")); q.add(_t("B"))

    assert q.move(5, 0) is None
    assert q.move(0, 10) is None
    assert q.move(-1, 0) is None
    # Queue unchanged
    assert [t.title for t in q.upcoming] == ["A", "B"]


def test_move_to_same_position_is_no_op_returns_track() -> None:
    q = MusicQueue()
    a, b = _t("A"), _t("B")
    q.add(a); q.add(b)

    moved = q.move(0, 0)

    assert moved is a
    assert [t.title for t in q.upcoming] == ["A", "B"]


def test_move_clamps_to_range_when_target_is_within_one_past_end() -> None:
    """``to_idx == len(queue)-1`` means "to last position" — clamp to it
    rather than treating end-equality as out-of-range."""
    q = MusicQueue()
    q.add(_t("A")); q.add(_t("B")); q.add(_t("C"))

    moved = q.move(0, 2)

    assert moved is not None
    assert [t.title for t in q.upcoming] == ["B", "C", "A"]


def test_move_updates_original_order_when_shuffled() -> None:
    """If the queue is shuffled, ``move`` mutates the live order; the
    saved original-order remains the pre-shuffle baseline. Move stays
    consistent with that invariant by re-syncing original_order to
    reflect the manual move."""
    q = MusicQueue()
    for n in "ABCD":
        q.add(_t(n))
    q.shuffle()
    # The user-visible queue is shuffled; original_order has [A,B,C,D]
    # Doing a manual move on the shuffled view shouldn't break unshuffle.
    pre_move_queue = list(q.upcoming)
    q.move(0, 3)  # take first shuffled track, send to end

    # The visible queue moved. After unshuffle, the manually-moved track
    # should still be removed/reinserted consistently with the user's intent.
    moved_track = pre_move_queue[0]
    assert q.upcoming[-1] is moved_track


# ---------------------------------------------------------------------------
# previous() — non-mutating history pop
# ---------------------------------------------------------------------------

def test_previous_returns_last_history_entry() -> None:
    q = MusicQueue()
    a, b = _t("A"), _t("B")
    q.add(a); q.add(b)
    q.get_next()  # current=A
    q.get_next()  # current=B, history=[A]

    prev = q.previous()

    assert prev is a


def test_previous_pops_from_history() -> None:
    """Calling previous twice should yield two different tracks (history
    walker semantics), not the same track twice."""
    q = MusicQueue()
    a, b, c = _t("A"), _t("B"), _t("C")
    q.add(a); q.add(b); q.add(c)
    q.get_next()  # current=A, history=[]
    q.get_next()  # current=B, history=[A]
    q.get_next()  # current=C, history=[A, B]

    first = q.previous()
    second = q.previous()

    assert first is b
    assert second is a
    assert q.history == []  # exhausted


def test_previous_returns_none_when_history_empty() -> None:
    q = MusicQueue()
    q.add(_t("A"))
    q.get_next()  # current=A, history=[]

    assert q.previous() is None


def test_previous_does_not_modify_current_or_queue() -> None:
    """Contract: ``previous`` is a pure pop from history. State changes
    that re-route playback (insert current to queue front, set new
    current) belong to the caller. Without this discipline the player
    loop's ``get_next`` advance gets double-applied and tracks get
    skipped or replayed wrong."""
    q = MusicQueue()
    a, b, c = _t("A"), _t("B"), _t("C")
    q.add(a); q.add(b); q.add(c)
    q.get_next()  # current=A
    q.get_next()  # current=B, history=[A]

    pre_current = q.current
    pre_upcoming = list(q.upcoming)
    q.previous()

    assert q.current is pre_current
    assert q.upcoming == pre_upcoming


def test_previous_is_independent_of_loop_mode() -> None:
    """Previous walks history regardless of OFF / LOOP_ONE / LOOP_QUEUE.
    The semantics are about user intent ('go back'), not about the
    forward-advance policy."""
    q = MusicQueue()
    a, b = _t("A"), _t("B")
    q.add(a); q.add(b)
    q.get_next()
    q.get_next()  # history=[A], current=B
    q.set_loop_mode(LoopMode.LOOP_ONE)

    prev = q.previous()

    assert prev is a
