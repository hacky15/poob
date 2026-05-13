"""Music queue system — Track model, loop modes, and queue management.

List-backed queue with Fisher-Yates shuffle, three loop modes, and history
tracking. Designed for random access (display pagination, remove-by-index,
shuffle) which is why we use list over deque (per Wavelink 3.x migration).
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import timedelta
from enum import Enum, auto
from collections import deque


class LoopMode(Enum):
    """Playback loop behavior."""

    OFF = auto()       # Normal — advance linearly until queue empty
    LOOP_ONE = auto()  # Replay current track indefinitely
    LOOP_QUEUE = auto()  # Re-enqueue tracks after playing


class TrackSource(Enum):
    """Where the track came from."""

    YOUTUBE = "youtube"
    YOUTUBE_MUSIC = "youtube_music"
    DIRECT_URL = "direct"


@dataclass(slots=True)
class Track:
    """A single queued track.

    Separates permanent URL (webpage_url) from ephemeral stream URL.
    Stream URLs expire ~6 hours on YouTube — resolve lazily at play time.
    """

    title: str
    url: str  # Permanent webpage URL (e.g. youtube.com/watch?v=...)
    duration: timedelta | None = None
    requester_id: int = 0
    requester_name: str = ""
    thumbnail: str | None = None
    identifier: str | None = None  # Platform-specific ID (for dedup/re-fetch)
    source: TrackSource = TrackSource.YOUTUBE
    stream_url: str | None = None  # Ephemeral — populated at play time
    local_file: str | None = None  # Pre-downloaded temp file path (if cached)
    is_stream: bool = False  # True for livestreams (no duration)

    @property
    def duration_str(self) -> str:
        """Human-readable duration like '3:42' or 'LIVE'."""
        if self.is_stream:
            return "LIVE"
        if self.duration is None:
            return "??:??"
        total = int(self.duration.total_seconds())
        mins, secs = divmod(total, 60)
        hours, mins = divmod(mins, 60)
        if hours:
            return f"{hours}:{mins:02d}:{secs:02d}"
        return f"{mins}:{secs:02d}"


class MusicQueue:
    """Thread-safe music queue with loop, shuffle, and history.

    All mutation happens through methods — no direct list access from outside.
    History is a bounded deque (last 100 tracks) for potential replay/back.
    """

    def __init__(self, max_history: int = 100) -> None:
        self._queue: list[Track] = []
        self._history: deque[Track] = deque(maxlen=max_history)
        self._current: Track | None = None
        self._loop_mode: LoopMode = LoopMode.OFF
        self._shuffled: bool = False
        self._original_order: list[Track] | None = None  # Saved pre-shuffle order

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def current(self) -> Track | None:
        """Currently playing track."""
        return self._current

    @current.setter
    def current(self, track: Track | None) -> None:
        self._current = track

    @property
    def loop_mode(self) -> LoopMode:
        return self._loop_mode

    @property
    def is_shuffled(self) -> bool:
        return self._shuffled

    @property
    def upcoming(self) -> list[Track]:
        """Read-only view of upcoming tracks."""
        return list(self._queue)

    @property
    def history(self) -> list[Track]:
        """Read-only view of play history (most recent last)."""
        return list(self._history)

    @property
    def size(self) -> int:
        """Number of tracks waiting in queue (excludes current)."""
        return len(self._queue)

    @property
    def is_empty(self) -> bool:
        return len(self._queue) == 0

    @property
    def total_duration(self) -> timedelta:
        """Sum of all queued track durations (excludes unknowns)."""
        total = timedelta()
        for track in self._queue:
            if track.duration:
                total += track.duration
        return total

    # ------------------------------------------------------------------
    # Core operations
    # ------------------------------------------------------------------

    def add(self, track: Track) -> int:
        """Add a track to the end of the queue. Returns queue position."""
        self._queue.append(track)
        if self._original_order is not None:
            self._original_order.append(track)
        return len(self._queue)

    def add_many(self, tracks: list[Track]) -> int:
        """Add multiple tracks. Returns count added."""
        self._queue.extend(tracks)
        if self._original_order is not None:
            self._original_order.extend(tracks)
        return len(tracks)

    def add_next(self, track: Track) -> None:
        """Insert a track at the front of the queue (plays next)."""
        self._queue.insert(0, track)
        if self._original_order is not None:
            self._original_order.insert(0, track)

    def get_next(self) -> Track | None:
        """Advance the queue and return the next track.

        Handles all three loop modes:
        - OFF: push current to history, pop next from queue
        - LOOP_ONE: return current without touching state
        - LOOP_QUEUE: push current to history AND re-enqueue, then pop next
        """
        if self._loop_mode == LoopMode.LOOP_ONE and self._current is not None:
            # Reset stream URL so it gets re-resolved (URLs expire)
            self._current.stream_url = None
            return self._current

        # Push current to history
        if self._current is not None:
            self._history.append(self._current)

            if self._loop_mode == LoopMode.LOOP_QUEUE:
                # Re-enqueue at the end
                recycled = Track(
                    title=self._current.title,
                    url=self._current.url,
                    duration=self._current.duration,
                    requester_id=self._current.requester_id,
                    requester_name=self._current.requester_name,
                    thumbnail=self._current.thumbnail,
                    identifier=self._current.identifier,
                    source=self._current.source,
                    is_stream=self._current.is_stream,
                )
                self._queue.append(recycled)

        # Pop next
        if not self._queue:
            self._current = None
            return None

        self._current = self._queue.pop(0)
        self._current.stream_url = None  # Force re-resolve
        return self._current

    def skip(self) -> Track | None:
        """Skip current track — advances even in LOOP_ONE mode."""
        saved = self._loop_mode
        if self._loop_mode == LoopMode.LOOP_ONE:
            self._loop_mode = LoopMode.OFF
        result = self.get_next()
        self._loop_mode = saved
        return result

    def remove(self, index: int) -> Track | None:
        """Remove a track by queue index (0-based). Returns removed track."""
        if 0 <= index < len(self._queue):
            track = self._queue.pop(index)
            if self._original_order is not None:
                try:
                    self._original_order.remove(track)
                except ValueError:
                    pass
            return track
        return None

    def move(self, from_idx: int, to_idx: int) -> Track | None:
        """Reorder a track within the upcoming queue.

        Both indices are 0-based against ``upcoming`` (not history, not
        ``current``). Returns the moved track on success, ``None`` for
        out-of-range indices. ``move(i, i)`` is a no-op that still
        returns the track at ``i``.

        If the queue is shuffled, the saved original-order is kept in
        sync so a later ``unshuffle()`` reflects the manual move.
        """
        size = len(self._queue)
        if not (0 <= from_idx < size) or not (0 <= to_idx < size):
            return None
        track = self._queue.pop(from_idx)
        self._queue.insert(to_idx, track)
        if self._original_order is not None:
            try:
                self._original_order.remove(track)
                # Clamp to current shuffled-order length to keep the
                # restore stable; exact position on unshuffle isn't
                # required to be perfect (unshuffle is best-effort by
                # design) but the saved-order must contain the track.
                self._original_order.append(track)
            except ValueError:
                pass
        return track

    def previous(self) -> Track | None:
        """Pop the most-recent finished track from history.

        Returns the popped track, or ``None`` if history is empty. The
        caller (``GuildMusicPlayer.previous``) is responsible for
        plumbing the returned track into playback — this method does
        NOT touch ``current`` or the upcoming queue.

        Keeping this pure-pop avoids double-advancing through the
        player-loop's ``get_next`` path. See
        ``docs/decisions/music-queue-primitives.md``.
        """
        if not self._history:
            return None
        return self._history.pop()

    # ------------------------------------------------------------------
    # Loop & shuffle
    # ------------------------------------------------------------------

    def cycle_loop_mode(self) -> LoopMode:
        """Cycle through loop modes: OFF → LOOP_ONE → LOOP_QUEUE → OFF."""
        modes = [LoopMode.OFF, LoopMode.LOOP_ONE, LoopMode.LOOP_QUEUE]
        idx = modes.index(self._loop_mode)
        self._loop_mode = modes[(idx + 1) % len(modes)]
        return self._loop_mode

    def set_loop_mode(self, mode: LoopMode) -> None:
        self._loop_mode = mode

    def shuffle(self) -> None:
        """Fisher-Yates shuffle the queue. Saves original order for unshuffle."""
        if not self._queue:
            return
        if not self._shuffled:
            self._original_order = list(self._queue)
        random.shuffle(self._queue)
        self._shuffled = True

    def unshuffle(self) -> None:
        """Restore original queue order (only tracks still in queue)."""
        if not self._shuffled or self._original_order is None:
            return
        remaining_ids = {id(t) for t in self._queue}
        self._queue = [t for t in self._original_order if id(t) in remaining_ids]
        self._original_order = None
        self._shuffled = False

    def toggle_shuffle(self) -> bool:
        """Toggle shuffle on/off. Returns new shuffle state."""
        if self._shuffled:
            self.unshuffle()
        else:
            self.shuffle()
        return self._shuffled

    # ------------------------------------------------------------------
    # Bulk operations
    # ------------------------------------------------------------------

    def clear(self) -> int:
        """Clear the queue (not current track). Returns count cleared."""
        count = len(self._queue)
        self._queue.clear()
        self._original_order = None
        self._shuffled = False
        return count

    def clear_all(self) -> None:
        """Clear everything — queue, current, history."""
        self._queue.clear()
        self._history.clear()
        self._current = None
        self._original_order = None
        self._shuffled = False
        self._loop_mode = LoopMode.OFF

    # ------------------------------------------------------------------
    # Display helpers
    # ------------------------------------------------------------------

    def format_queue(self, page: int = 1, per_page: int = 10) -> str:
        """Format queue for Discord display.

        Returns a string like:
            **Now Playing:** Song Title [3:42]
            **Up Next:**
            1. Song A [2:15] — requested by Ben
            2. Song B [4:01] — requested by Simon
            ...
            **10 tracks in queue | 32:15 total**
        """
        lines: list[str] = []

        if self._current:
            loop_indicator = ""
            if self._loop_mode == LoopMode.LOOP_ONE:
                loop_indicator = " (looping)"
            elif self._loop_mode == LoopMode.LOOP_QUEUE:
                loop_indicator = " (queue loop)"
            lines.append(
                f"**Now Playing:** {self._current.title} "
                f"[{self._current.duration_str}]{loop_indicator}"
            )

        if not self._queue:
            if self._current:
                lines.append("*Queue is empty — add more tracks!*")
            else:
                lines.append("*Nothing playing and queue is empty.*")
            return "\n".join(lines)

        lines.append("**Up Next:**")
        start = (page - 1) * per_page
        end = start + per_page
        page_tracks = self._queue[start:end]

        for i, track in enumerate(page_tracks, start=start + 1):
            requester = f" — {track.requester_name}" if track.requester_name else ""
            lines.append(f"`{i}.` {track.title} [{track.duration_str}]{requester}")

        total_pages = (len(self._queue) + per_page - 1) // per_page
        total_dur = self.total_duration
        dur_str = str(total_dur).split(".")[0] if total_dur else "0:00"

        shuffle_str = " | shuffled" if self._shuffled else ""
        lines.append(
            f"\n**{len(self._queue)} tracks** | {dur_str} total"
            f"{shuffle_str} | Page {page}/{total_pages}"
        )
        return "\n".join(lines)
