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
import os
import re
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from typing import TYPE_CHECKING

from poob.music.queue import Track, TrackSource
from poob.utils.logging import get_logger

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

# FFmpeg options for streaming playback (livestreams, download failures).
# Local file playback uses minimal flags — see player._make_audio_source().
FFMPEG_BEFORE_OPTS = (
    "-nostdin "
    "-probesize 1000000 "
    "-analyzeduration 0 "
    "-reconnect 1 "
    "-reconnect_streamed 1 "
    "-reconnect_on_network_error 1 "
    "-reconnect_delay_max 5"
)
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
        """Search YouTube and return the best-matching result as a Track.

        Handles both direct URLs and text search queries (via
        default_search: auto). Two-pass best-guess for non-URL queries
        (see [[ytdl-search-best-guess-fallback]]), now with candidate
        RE-RANKING on the widened pass (see
        [[music-search-candidate-rerank]]):

        1. Pass 1: ``extract_info(query)`` (→ ytsearch1). Returned as-is
           when the hit looks plausible for the query.
        2. Pass 2: when pass 1 came back empty OR its hit looks
           implausible (long-form video for a song-shaped query — the
           "31-minute pottery video" failure), widen to ``ytsearch5``
           and pick the highest-scoring candidate by title-token
           overlap + duration fit. The pass-1 hit competes in the
           ranking (with a small incumbent bonus), so the outcome is
           never worse than take-first — best-guess still beats
           dead-end, it's just a better guess.

        Direct URLs skip both the fallback and the plausibility check —
        the URL is the source of truth.
        """
        info = await self.extract_info(query)
        track = self._first_track_from_info(info, requester_id, requester_name)
        if track is not None and not self._is_implausible_hit(query, track):
            return track

        # Don't widen for direct URLs — we know what they wanted.
        if self._looks_like_url(query):
            return track

        # Best-guess fallback: ytsearch5 picks up near-matches that
        # ytsearch1's exact-title pass misses; re-rank picks the most
        # song-plausible candidate instead of blindly taking the first.
        log.info(
            "ytdl.search widening to ytsearch5",
            query=query[:80],
            reason="empty" if track is None else "implausible_top_hit",
        )
        widened = await self.extract_info(f"ytsearch5:{query}")
        candidates: list[Track] = []
        if widened is not None:
            entries = widened.get("entries") if "entries" in widened else [widened]
            for entry in entries or []:
                if entry is None:
                    continue
                candidates.append(self._info_to_track(entry, requester_id, requester_name))
        if track is not None:
            candidates.append(track)
        if not candidates:
            return None

        # Incumbent bonus: the pass-1 hit wins ties so a plausible-enough
        # original pick isn't churned for a marginally-scored alternative.
        def _key(cand: Track) -> float:
            bonus = 0.1 if cand is track else 0.0
            return self._relevance_score(query, cand) + bonus

        best = max(candidates, key=_key)
        log.info(
            "ytdl.search picked via rerank",
            query=query[:80],
            picked=best.title[:80],
            candidates=len(candidates),
        )
        return best

    @staticmethod
    def _looks_like_url(query: str) -> bool:
        """Quick check for direct URL — skips the fallback path."""
        q = query.strip().lower()
        return q.startswith(("http://", "https://", "www.")) or "youtube.com" in q or "youtu.be" in q

    # ------------------------------------------------------------------
    # Candidate re-ranking (see docs/decisions/music-search-candidate-rerank)
    # ------------------------------------------------------------------

    # Query words that signal the user WANTS long-form content — duration
    # scoring is disabled for these so a 2-hour mix isn't penalized.
    _LONGFORM_QUERY_SIGNALS = frozenset(
        {
            "mix",
            "mixtape",
            "playlist",
            "compilation",
            "album",
            "megamix",
            "mashup",
            "medley",
            "hour",
            "hours",
            "podcast",
            "audiobook",
            "asmr",
            "radio",
            "session",
            "sessions",
            "marathon",
            "sleep",
            "study",
            "lofi",
            "lo-fi",
            "episode",
            "full",
            "concert",
        }
    )

    # Dropped before overlap scoring — connectives and video-title chrome
    # that would inflate matches without carrying song identity.
    _QUERY_STOPWORDS = frozenset(
        {
            "the",
            "a",
            "an",
            "by",
            "of",
            "and",
            "feat",
            "ft",
            "featuring",
            "official",
            "video",
            "audio",
            "lyrics",
            "lyric",
            "song",
            "music",
        }
    )

    # A pass-1 hit longer than this for a song-shaped query triggers the
    # widened re-rank pass. Matches MAX_PREDOWNLOAD_DURATION_SEC — the
    # system already treats >15 min as "not a normal song" for downloads.
    LONGFORM_HIT_THRESHOLD_SEC = 900

    @classmethod
    def _query_tokens(cls, text: str) -> set[str]:
        """Lowercased content tokens with stopwords removed."""
        return {t for t in re.findall(r"[a-z0-9']+", text.lower()) if t not in cls._QUERY_STOPWORDS}

    @classmethod
    def _has_longform_intent(cls, query: str) -> bool:
        """True when the query itself asks for long-form content."""
        return bool(
            {t for t in re.findall(r"[a-z0-9'-]+", query.lower())} & cls._LONGFORM_QUERY_SIGNALS
        )

    @classmethod
    def _relevance_score(cls, query: str, track: Track) -> float:
        """Score a candidate for a search query — higher is better.

        Title-token overlap (0..1) plus a duration-fit adjustment for
        song-shaped queries: typical song lengths get a bonus, long-form
        results a growing penalty. Queries with long-form intent skip
        the duration term entirely.
        """
        qtokens = cls._query_tokens(query)
        ttokens = cls._query_tokens(track.title)
        overlap = len(qtokens & ttokens) / len(qtokens) if qtokens else 0.0
        score = overlap

        if cls._has_longform_intent(query):
            return score

        if track.duration is None:
            # Livestreams for a song-shaped query are almost never the ask.
            return score - 0.25 if track.is_stream else score

        seconds = track.duration.total_seconds()
        if 60 <= seconds <= 900:
            score += 0.3  # typical song length
        elif seconds > 1800:
            score -= 0.5  # 30-minute-plus long-form
        elif seconds > 900:
            score -= 0.2  # mildly long
        return score

    def _is_implausible_hit(self, query: str, track: Track) -> bool:
        """True when a pass-1 hit warrants the widened re-rank pass.

        Narrow by design: only fires on long-form results (>15 min) for
        queries that did not ask for long-form content, and never for
        direct URLs (the user picked that video themselves).
        """
        if self._looks_like_url(query):
            return False
        if track.duration is None:
            return False
        if track.duration.total_seconds() <= self.LONGFORM_HIT_THRESHOLD_SEC:
            return False
        return not self._has_longform_intent(query)

    def _first_track_from_info(
        self,
        info: dict | None,
        requester_id: int,
        requester_name: str,
    ) -> Track | None:
        """Extract the first viable Track from a yt-dlp info dict.

        Handles both single-result and entries-list shapes. Returns
        None if no entry is usable.
        """
        if info is None:
            return None
        if "entries" in info:
            entries = list(info["entries"])
            for entry in entries:
                if entry is None:
                    continue
                return self._info_to_track(entry, requester_id, requester_name)
            return None
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

    # Pre-download guardrails: anything bigger/longer than this streams instead
    MAX_PREDOWNLOAD_DURATION_SEC = 900     # 15 minutes
    MAX_PREDOWNLOAD_FILESIZE = 100_000_000  # 100 MB (yt-dlp hard cap)

    async def download_track(self, track: Track, *, timeout: float = 30.0) -> str | None:
        """Pre-download a track's audio to a temp file for local playback.

        Eliminates YouTube TLS termination issues entirely — FFmpeg reads
        from a local file instead of a network stream. Downloads are fast
        (2-5s for typical tracks) because yt-dlp fetches audio-only.

        Guardrails (prevent multi-GB lo-fi compilation downloads):
        - Skips tracks over MAX_PREDOWNLOAD_DURATION_SEC → stream instead
        - Caps audio file size at MAX_PREDOWNLOAD_FILESIZE via yt-dlp
        - Skips livestreams (must be streamed)
        - Returns cached path if already downloaded

        Returns:
            Path to the downloaded temp file, or None on failure/skip.
            The path is also stored on track.local_file.
        """
        if track.is_stream:
            return None  # Livestreams can't be pre-downloaded
        if track.local_file and os.path.isfile(track.local_file):
            return track.local_file  # Already cached

        # Duration gate: long videos (compilations, 1-hour mixes) are too
        # big to pre-download. Stream them from YouTube directly.
        if track.duration and track.duration.total_seconds() > self.MAX_PREDOWNLOAD_DURATION_SEC:
            log.info(
                "Skipping pre-download (too long, will stream)",
                title=track.title[:60],
                duration_sec=int(track.duration.total_seconds()),
            )
            return None

        loop = asyncio.get_running_loop()

        # Shared state so timeout cleanup can find the partial file
        # even after the blocking yt-dlp thread has moved on.
        state: dict[str, str | None] = {"tmp_path": None, "result": None}

        def _download() -> str | None:
            import yt_dlp

            # Download to a temp file — caller is responsible for cleanup
            fd, tmp_path = tempfile.mkstemp(suffix=".webm", prefix="poob_music_")
            os.close(fd)
            state["tmp_path"] = tmp_path

            dl_opts = {
                # Prefer small-to-medium audio formats, fall back to best.
                # Hard cap via max_filesize prevents multi-GB downloads.
                "format": "bestaudio[filesize<100M]/bestaudio/best",
                "max_filesize": self.MAX_PREDOWNLOAD_FILESIZE,
                "outtmpl": tmp_path,
                "quiet": True,
                "no_warnings": True,
                "source_address": "0.0.0.0",
                "nocheckcertificate": True,
                "overwrites": True,
            }
            if self._extract_opts.get("cookiefile"):
                dl_opts["cookiefile"] = self._extract_opts["cookiefile"]

            try:
                with yt_dlp.YoutubeDL(dl_opts) as ydl:
                    ydl.download([track.url])
                # yt-dlp may change the extension
                base = os.path.splitext(tmp_path)[0]
                for candidate in [tmp_path, base + ".opus", base + ".m4a",
                                  base + ".webm", base + ".mp3"]:
                    if os.path.isfile(candidate) and os.path.getsize(candidate) > 0:
                        state["result"] = candidate
                        return candidate
                if os.path.isfile(tmp_path) and os.path.getsize(tmp_path) > 0:
                    state["result"] = tmp_path
                    return tmp_path
                return None
            except Exception:
                # yt-dlp raises on max_filesize exceeded — clean up partial
                _cleanup_partial(tmp_path)
                return None

        def _cleanup_partial(base_path: str) -> None:
            """Remove any partial file yt-dlp may have created."""
            base = os.path.splitext(base_path)[0]
            for candidate in [base_path, base + ".opus", base + ".m4a",
                              base + ".webm", base + ".mp3",
                              base_path + ".part", base + ".webm.part"]:
                try:
                    if os.path.isfile(candidate):
                        os.unlink(candidate)
                except OSError:
                    pass

        try:
            result = await asyncio.wait_for(
                loop.run_in_executor(self._executor, _download),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            log.warning("Track download timed out", title=track.title[:60])
            # The thread keeps running — schedule cleanup of any file it
            # writes so we don't orphan 100MB+ partials on disk.
            tmp_path = state.get("tmp_path")
            if tmp_path:
                loop.run_in_executor(
                    self._executor,
                    lambda: (_cleanup_partial(tmp_path)),
                )
            return None
        except Exception as exc:
            log.warning("Track download failed", title=track.title[:60], error=str(exc)[:120])
            return None

        if result:
            track.local_file = result
            size_mb = os.path.getsize(result) / 1_000_000
            log.info("Track pre-downloaded", title=track.title[:50],
                     file=result, size_mb=f"{size_mb:.1f}")
        return result

    @staticmethod
    def cleanup_track_file(track: Track) -> None:
        """Remove a track's pre-downloaded temp file if it exists."""
        if track.local_file:
            try:
                os.unlink(track.local_file)
            except OSError:
                pass
            track.local_file = None

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
