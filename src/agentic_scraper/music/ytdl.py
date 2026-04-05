"""Async yt-dlp wrapper — search, extract, and playlist handling.

All yt-dlp calls run in a dedicated ThreadPoolExecutor (3 workers) to avoid
blocking the asyncio event loop. URLs are extracted lazily — playlists store
only metadata, stream URLs resolve at play time (they expire ~6 hours).

Architecture:
    User query → search/extract → Track metadata (permanent URL)
    Play time → resolve_stream_url → ephemeral googlevideo.com URL → FFmpeg
"""

from __future__ import annotations

import asyncio
import functools
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from typing import TYPE_CHECKING

from agentic_scraper.music.queue import Track, TrackSource
from agentic_scraper.utils.logging import get_logger

if TYPE_CHECKING:
    pass

log = get_logger("music.ytdl")

# ---------------------------------------------------------------------------
# yt-dlp configuration
# ---------------------------------------------------------------------------

# Extraction options — metadata only, never download
YDL_EXTRACT_OPTS: dict = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "default_search": "auto",  # Auto-detect URL vs search query
    "source_address": "0.0.0.0",  # Bind to all interfaces
    "nocheckcertificate": True,
    "extract_flat": False,
}

# Playlist-specific options — flat extraction (metadata only, no stream URLs)
YDL_PLAYLIST_OPTS: dict = {
    "extract_flat": "in_playlist",
    "quiet": True,
    "no_warnings": True,
    "source_address": "0.0.0.0",
}

# FFmpeg options for streaming playback — reconnect flags are NON-NEGOTIABLE
FFMPEG_BEFORE_OPTS = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
FFMPEG_OPTS = "-vn"


# ---------------------------------------------------------------------------
# AsyncYTDL
# ---------------------------------------------------------------------------


class AsyncYTDL:
    """Async wrapper around yt-dlp for non-blocking YouTube operations.

    Uses a dedicated ThreadPoolExecutor (not the default) to prevent
    yt-dlp from saturating shared thread pools. All extract_info calls
    are I/O-bound (network requests), so threads provide full concurrency.

    Args:
        cookie_file: Path to Netscape-format cookies.txt for age-restricted
            content. Firefox recommended (Chrome encrypts cookies).
        max_workers: Thread pool size. 3-4 is plenty — yt-dlp is I/O-bound.
    """

    def __init__(
        self,
        cookie_file: str = "",
        max_workers: int = 3,
    ) -> None:
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="ytdl",
        )

        # Build options with optional cookie auth
        self._extract_opts = dict(YDL_EXTRACT_OPTS)
        self._playlist_opts = dict(YDL_PLAYLIST_OPTS)
        if cookie_file:
            self._extract_opts["cookiefile"] = cookie_file
            self._playlist_opts["cookiefile"] = cookie_file

        # Lazy-init yt-dlp instances (import is heavy)
        self._ytdl = None
        self._ytdl_playlist = None

    def _get_ytdl(self):
        """Lazy-init the main YoutubeDL instance."""
        if self._ytdl is None:
            import yt_dlp
            self._ytdl = yt_dlp.YoutubeDL(self._extract_opts)
        return self._ytdl

    def _get_ytdl_playlist(self):
        """Lazy-init the playlist YoutubeDL instance."""
        if self._ytdl_playlist is None:
            import yt_dlp
            self._ytdl_playlist = yt_dlp.YoutubeDL(self._playlist_opts)
        return self._ytdl_playlist

    # ------------------------------------------------------------------
    # Core extraction
    # ------------------------------------------------------------------

    async def extract_info(
        self,
        query: str,
        *,
        download: bool = False,
        timeout: float = 30.0,
    ) -> dict | None:
        """Extract info for a URL or search query.

        Runs in executor to avoid blocking the event loop.
        Returns the yt-dlp info dict, or None on failure.
        """
        loop = asyncio.get_running_loop()
        ytdl = self._get_ytdl()
        func = functools.partial(ytdl.extract_info, query, download=download)
        try:
            return await asyncio.wait_for(
                loop.run_in_executor(self._executor, func),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            log.warning("yt-dlp extraction timed out", query=query[:80], timeout=timeout)
            return None
        except Exception as exc:
            log.warning("yt-dlp extraction failed", query=query[:80], error=str(exc)[:120])
            return None

    async def extract_playlist(
        self,
        url: str,
        *,
        timeout: float = 30.0,
    ) -> tuple[str, list[dict]]:
        """Extract playlist metadata (flat — titles and IDs only).

        Returns (playlist_title, entries_list). Each entry has at minimum:
        title, id, url (webpage), duration.
        """
        loop = asyncio.get_running_loop()
        ytdl = self._get_ytdl_playlist()
        func = functools.partial(ytdl.extract_info, url, download=False)
        try:
            info = await asyncio.wait_for(
                loop.run_in_executor(self._executor, func),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            log.warning("Playlist extraction timed out", url=url[:80])
            return ("Unknown Playlist", [])
        except Exception as exc:
            log.warning("Playlist extraction failed", url=url[:80], error=str(exc)[:120])
            return ("Unknown Playlist", [])

        if info is None:
            return ("Unknown Playlist", [])

        title = info.get("title", "Unknown Playlist")
        entries = list(info.get("entries", []))
        return (title, entries)

    # ------------------------------------------------------------------
    # High-level operations
    # ------------------------------------------------------------------

    async def search(
        self,
        query: str,
        *,
        requester_id: int = 0,
        requester_name: str = "",
    ) -> Track | None:
        """Search YouTube and return the top result as a Track.

        Handles both direct URLs and text search queries (via default_search: auto).
        """
        info = await self.extract_info(query)
        if info is None:
            return None

        # If it's a playlist result, take the first entry
        if "entries" in info:
            entries = list(info["entries"])
            if not entries:
                return None
            info = entries[0]

        return self._info_to_track(info, requester_id, requester_name)

    async def search_many(
        self,
        query: str,
        count: int = 5,
        *,
        requester_id: int = 0,
        requester_name: str = "",
    ) -> list[Track]:
        """Search YouTube and return multiple results."""
        search_query = f"ytsearch{count}:{query}"
        info = await self.extract_info(search_query)
        if info is None:
            return []

        entries = list(info.get("entries", []))
        return [
            self._info_to_track(e, requester_id, requester_name)
            for e in entries
            if e is not None
        ]

    async def get_playlist_tracks(
        self,
        url: str,
        *,
        requester_id: int = 0,
        requester_name: str = "",
    ) -> tuple[str, list[Track]]:
        """Extract a playlist into Track objects (metadata only).

        Stream URLs are NOT resolved — they'll be resolved at play time.
        This makes playlist ingestion near-instant regardless of size.
        """
        title, entries = await self.extract_playlist(url)
        tracks: list[Track] = []
        for entry in entries:
            if entry is None:
                continue
            video_url = entry.get("url") or f"https://www.youtube.com/watch?v={entry.get('id', '')}"
            dur = entry.get("duration")
            tracks.append(Track(
                title=entry.get("title", "Unknown"),
                url=video_url,
                duration=timedelta(seconds=dur) if dur else None,
                requester_id=requester_id,
                requester_name=requester_name,
                identifier=entry.get("id"),
                thumbnail=entry.get("thumbnail"),
                source=TrackSource.YOUTUBE,
            ))
        log.info("Playlist extracted", title=title, tracks=len(tracks))
        return (title, tracks)

    async def resolve_stream_url(self, track: Track) -> str | None:
        """Resolve a fresh stream URL for a track at play time.

        YouTube stream URLs expire ~6 hours. This re-extracts the
        ephemeral googlevideo.com URL right before playback.
        """
        info = await self.extract_info(track.url)
        if info is None:
            return None

        if "entries" in info:
            entries = list(info["entries"])
            if not entries:
                return None
            info = entries[0]

        stream_url = info.get("url")
        if stream_url:
            track.stream_url = stream_url
            # Update metadata if richer data is available
            if info.get("title") and track.title in ("Unknown", ""):
                track.title = info["title"]
            if info.get("duration") and track.duration is None:
                track.duration = timedelta(seconds=info["duration"])
            if info.get("thumbnail") and track.thumbnail is None:
                track.thumbnail = info["thumbnail"]

        return stream_url

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _info_to_track(
        self,
        info: dict,
        requester_id: int,
        requester_name: str,
    ) -> Track:
        """Convert a yt-dlp info dict to a Track."""
        dur = info.get("duration")
        is_live = info.get("is_live", False)

        # Determine source
        source = TrackSource.YOUTUBE
        extractor = info.get("extractor", "").lower()
        if "music" in extractor:
            source = TrackSource.YOUTUBE_MUSIC

        return Track(
            title=info.get("title", "Unknown"),
            url=info.get("webpage_url") or info.get("url", ""),
            stream_url=info.get("url"),  # May be ephemeral — will re-resolve
            duration=timedelta(seconds=dur) if dur and not is_live else None,
            requester_id=requester_id,
            requester_name=requester_name,
            thumbnail=info.get("thumbnail"),
            identifier=info.get("id"),
            source=source,
            is_stream=is_live,
        )

    def _is_playlist_url(self, url: str) -> bool:
        """Check if a URL looks like a playlist."""
        return "list=" in url or "/playlist" in url

    def close(self) -> None:
        """Shut down the thread pool."""
        self._executor.shutdown(wait=False)
        log.info("AsyncYTDL executor shut down")
