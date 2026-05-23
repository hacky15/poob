"""Synced-lyrics resolver — LRCLIB cascade + plain-text fallback.

Translates a (title, artist) pair into a :class:`ParsedLyrics` view with
``(timestamp_seconds, line_text)`` tuples for the synced case and a single
``(0.0, full_text)`` entry for the plain-text fallback.

The ``syncedlyrics`` PyPI package is lazy-imported so unit tests can mock
the resolver without the dependency installed. LRCLIB is the only auth-
free provider; the cascade uses it for both the synced + plain tier.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field

from poob.utils.logging import get_logger

log = get_logger("music.lyrics")


# LRC timestamp regex: [mm:ss.xx] or [mm:ss] — most files use two-digit
# centiseconds. Anything else is treated as malformed and skipped.
_LRC_LINE_RE = re.compile(
    r"\[(?P<min>\d{1,3}):(?P<sec>\d{1,2})(?:\.(?P<cs>\d{1,3}))?\](?P<text>.*)",
)


def _parse_lrc(text: str) -> list[tuple[float, str]]:
    """Parse an LRC body into ``[(timestamp_seconds, text), ...]`` sorted by time.

    Malformed lines are skipped (logged at INFO at most, never raises).
    Empty text after the timestamp is preserved — LRC instrumental
    breaks legitimately use ``[mm:ss]`` with no body.
    """
    out: list[tuple[float, str]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        match = _LRC_LINE_RE.match(line)
        if match is None:
            continue
        minutes = int(match.group("min"))
        seconds = int(match.group("sec"))
        cs_str = match.group("cs") or "0"
        # Centiseconds may be 1, 2, or 3 digits — normalize to fractional second.
        cs_normalized = float(cs_str) / (10 ** len(cs_str))
        ts = minutes * 60 + seconds + cs_normalized
        body = (match.group("text") or "").strip()
        out.append((ts, body))
    out.sort(key=lambda pair: pair[0])
    return out


@dataclass
class ParsedLyrics:
    """Lyrics for one track, synced or plain.

    For synced lyrics, ``lines`` is a list of ``(timestamp_seconds, text)``
    tuples sorted by timestamp. For plain text, ``lines`` is a single
    entry ``(0.0, full_text)``.
    """

    title: str
    artist: str
    is_synced: bool
    lines: list[tuple[float, str]] = field(default_factory=list)

    def current_line_at(self, position_seconds: float) -> str | None:
        """Pick the last line whose timestamp <= ``position_seconds``.

        Returns ``None`` when ``position_seconds`` is before the first
        line's timestamp (intro / pre-vocal section) — callers can render
        an em-dash or instrumental-marker glyph in that case.
        """
        if not self.lines:
            return None
        current: str | None = None
        for ts, text in self.lines:
            if ts <= position_seconds:
                current = text
            else:
                break
        return current

    def upcoming_window(
        self,
        position_seconds: float,
        before: int = 1,
        after: int = 3,
    ) -> list[tuple[bool, str]]:
        """Window for the live embed: ``before`` lines back + current + ``after`` ahead.

        Each entry is ``(is_current, text)`` so the renderer can bold the
        current line. Returns ``[]`` when there are no lines.
        """
        if not self.lines:
            return []
        # Find the index of the current line.
        current_index: int | None = None
        for i, (ts, _text) in enumerate(self.lines):
            if ts <= position_seconds:
                current_index = i
            else:
                break
        if current_index is None:
            # Before any line fires — show the first `after+1` lines, none current.
            head = self.lines[: max(after + 1, 1)]
            return [(False, text) for _ts, text in head]
        start = max(0, current_index - before)
        end = min(len(self.lines), current_index + after + 1)
        window = self.lines[start:end]
        result: list[tuple[bool, str]] = []
        for i, (_ts, text) in enumerate(window):
            is_current = (start + i) == current_index
            result.append((is_current, text))
        return result


class LyricsResolver:
    """Fetches lyrics for ``(title, artist)`` via the LRCLIB cascade.

    ``syncedlyrics`` is lazy-imported the first time :meth:`fetch` is
    actually called. Production wiring constructs one resolver per cog;
    tests can substitute a fake via the ``_search_func`` hook.
    """

    def __init__(self, _search_func=None) -> None:
        # Test hook — receives (query, providers, plain_only). Production
        # passes None; resolver lazy-imports syncedlyrics on first fetch.
        self._search_func = _search_func

    def _get_search(self):
        if self._search_func is not None:
            return self._search_func
        import syncedlyrics
        return lambda query, providers, plain_only: syncedlyrics.search(
            query, providers=providers, plain_only=plain_only,
        )

    async def fetch(
        self, title: str, artist: str | None = None,
    ) -> ParsedLyrics | None:
        """Resolve lyrics for the track. ``None`` if nothing was found.

        Cascade:
          1. LRCLIB synced (LRC format with timestamps)
          2. LRCLIB plain (unsynced text)
          3. None

        Every provider call is wrapped in ``try/except`` and runs under
        ``asyncio.to_thread`` because ``syncedlyrics`` is synchronous.
        """
        if not title:
            return None
        artist_clean = (artist or "").strip()
        query = f"{title} {artist_clean}".strip()
        search = self._get_search()

        # Tier 1: LRCLIB synced.
        synced_raw = await self._safe_search(search, query, ["Lrclib"], False)
        if synced_raw:
            lines = _parse_lrc(synced_raw)
            if lines:
                return ParsedLyrics(
                    title=title,
                    artist=artist_clean,
                    is_synced=True,
                    lines=lines,
                )

        # Tier 2: LRCLIB plain.
        plain_raw = await self._safe_search(search, query, ["Lrclib"], True)
        if plain_raw:
            return ParsedLyrics(
                title=title,
                artist=artist_clean,
                is_synced=False,
                lines=[(0.0, plain_raw.strip())],
            )

        return None

    @staticmethod
    async def _safe_search(
        search_func, query: str, providers: list[str], plain_only: bool,
    ) -> str | None:
        try:
            return await asyncio.to_thread(
                search_func, query, providers, plain_only,
            )
        except Exception as exc:
            log.warning(
                "lyrics.search failed",
                query=query[:80], plain_only=plain_only,
                error=str(exc)[:120],
            )
            return None
