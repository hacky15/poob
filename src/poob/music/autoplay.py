"""Autoplay cascade — generate the next track when the queue drains.

When a user enables autoplay on a guild and the queue empties, the player
loop calls :py:meth:`AutoplayEngine.get_next` with the most-recently-played
track as the seed. The engine tries three tiers in order, falling forward
on any failure, and returns a fresh ``Track`` (or ``None`` when every tier
gives up):

1. **ytmusicapi `get_watch_playlist(videoId=...)`** — YouTube Music's own
   recommender. Fast (<1 s) and high-quality on mainstream catalog. No
   auth required.
2. **yt-dlp on `https://www.youtube.com/watch?v=<id>&list=RD<id>`** — the
   plain-YouTube "mix" URL that yt-dlp can expand to a playlist of
   recommendations. Slower (1-3 s) but doesn't depend on the unofficial
   YT Music API.
3. **History shuffle** — pick a non-seed track from the caller-supplied
   history list. Last resort so playback doesn't dead-end on network /
   API outages.

The engine never raises — every tier's exception is caught and logged at
WARN with the message trimmed to 120 chars. Caller decides what to do on
``None`` (the existing queue-empty branch already breaks the loop).

See :doc:`docs/plans/music-autoplay.md` for the rationale and
:doc:`docs/research/music-bot-feature-roadmap.md` §6 for the cascade
ordering trade-offs.
"""

from __future__ import annotations

import asyncio
import random
from typing import Callable, Iterable, Protocol

from poob.music.queue import Track
from poob.utils.logging import get_logger

log = get_logger("music.autoplay")


# ---------------------------------------------------------------------------
# Protocols — keeps the engine testable without dragging in the real
# ytmusicapi / AsyncYTDL classes at unit-test time.
# ---------------------------------------------------------------------------


class _YTMusicLike(Protocol):
    def get_watch_playlist(
        self, *, videoId: str, limit: int = ...,
    ) -> dict: ...


class _YTDLLike(Protocol):
    async def search(self, query: str, **kwargs: object) -> Track | None: ...

    async def extract_playlist(
        self, url: str, **kwargs: object,
    ) -> tuple[str, list[dict]]: ...


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class AutoplayEngine:
    """Three-tier cascade generating a "next track" from a seed.

    The engine holds no state; the queue history is read fresh on every
    call via the ``history_accessor`` callable. Decoupling like this lets
    the player thread its own queue history in without the engine having
    to know about ``GuildMusicPlayer``.
    """

    def __init__(
        self,
        *,
        ytdl: _YTDLLike,
        history_accessor: Callable[[], Iterable[Track]],
        ytmusic_factory: Callable[[], _YTMusicLike] | None = None,
    ) -> None:
        self._ytdl = ytdl
        self._history_accessor = history_accessor
        # Lazy: build YTMusic only when we first need it, so import / auth
        # cost doesn't land on bot startup.
        self._ytmusic_factory = ytmusic_factory or _default_ytmusic_factory
        self._ytmusic: _YTMusicLike | None = None

    async def get_next(self, seed: Track | None) -> Track | None:
        """Return the next track for autoplay, or ``None`` if exhausted.

        Honors the cascade ordering documented at module level. Always
        returns — never raises.
        """
        if seed is None:
            return None

        # Tier 1: ytmusicapi.get_watch_playlist (only if we have a
        # YouTube video ID to seed with).
        if seed.identifier:
            try:
                track = await self._from_ytmusic(seed)
                if track is not None:
                    return track
            except Exception as exc:
                log.warning(
                    "autoplay ytmusic tier failed",
                    error=str(exc)[:120], seed=seed.title[:60],
                )

            # Tier 2: yt-dlp on the YouTube mix URL.
            try:
                track = await self._from_ytdl_mix(seed)
                if track is not None:
                    return track
            except Exception as exc:
                log.warning(
                    "autoplay ytdl-mix tier failed",
                    error=str(exc)[:120], seed=seed.title[:60],
                )

        # Tier 3: history shuffle. Always reachable.
        return self._from_history(seed)

    # -----------------------------------------------------------------
    # Tier 1 — ytmusicapi.get_watch_playlist
    # -----------------------------------------------------------------

    async def _from_ytmusic(self, seed: Track) -> Track | None:
        """Ask YT Music for a watch playlist seeded on ``seed.identifier``.

        Picks the first candidate whose video ID isn't the seed itself
        and isn't already in recent history.
        """
        if self._ytmusic is None:
            self._ytmusic = self._ytmusic_factory()

        # ytmusicapi is synchronous — run in the default executor to
        # avoid blocking the event loop.
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None,
            lambda: self._ytmusic.get_watch_playlist(
                videoId=seed.identifier or "", limit=10,
            ),
        )

        candidates = result.get("tracks", []) if isinstance(result, dict) else []
        recent_ids = self._recent_ids(seed)

        for cand in candidates:
            cand_id = cand.get("videoId")
            if not cand_id or cand_id == seed.identifier or cand_id in recent_ids:
                continue
            picked = await self._ytdl.search(
                f"https://youtu.be/{cand_id}",
            )
            if picked is not None:
                log.info("autoplay tier=ytmusic picked", title=picked.title[:60])
                return picked

        return None

    # -----------------------------------------------------------------
    # Tier 2 — yt-dlp on the YouTube RD<id> mix URL
    # -----------------------------------------------------------------

    async def _from_ytdl_mix(self, seed: Track) -> Track | None:
        """Expand the YouTube `RD<videoId>` mix URL via yt-dlp.

        yt-dlp returns a playlist info dict whose ``entries`` we iterate
        to find a fresh recommendation.
        """
        url = (
            f"https://www.youtube.com/watch?v={seed.identifier}"
            f"&list=RD{seed.identifier}"
        )
        _title, entries = await self._ytdl.extract_playlist(url)
        recent_ids = self._recent_ids(seed)

        for entry in entries:
            entry_id = entry.get("id") or entry.get("identifier")
            if not entry_id or entry_id == seed.identifier or entry_id in recent_ids:
                continue
            picked = await self._ytdl.search(
                f"https://youtu.be/{entry_id}",
            )
            if picked is not None:
                log.info("autoplay tier=ytdl-mix picked", title=picked.title[:60])
                return picked

        return None

    # -----------------------------------------------------------------
    # Tier 3 — history shuffle (last-resort)
    # -----------------------------------------------------------------

    def _from_history(self, seed: Track) -> Track | None:
        history = list(self._history_accessor())
        candidates = [
            t for t in history
            if not _same_track(t, seed)
        ]
        if not candidates:
            return None
        picked = random.choice(candidates)
        log.info("autoplay tier=history picked", title=picked.title[:60])
        return picked

    # -----------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------

    def _recent_ids(self, seed: Track) -> set[str]:
        ids = {t.identifier for t in self._history_accessor() if t.identifier}
        if seed.identifier:
            ids.add(seed.identifier)
        return ids


def _same_track(a: Track, b: Track) -> bool:
    if a.identifier and b.identifier:
        return a.identifier == b.identifier
    return a.url == b.url


def _default_ytmusic_factory() -> _YTMusicLike:
    """Lazy import so the bot can boot even if ytmusicapi is missing."""
    from ytmusicapi import YTMusic  # type: ignore[import-not-found]

    return YTMusic()
