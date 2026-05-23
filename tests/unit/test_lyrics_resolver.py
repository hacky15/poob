"""Tests for the synced-lyrics resolver.

Mocks ``syncedlyrics.search`` via the resolver's ``_search_func`` hook so
the suite runs without the package installed and without any network.
See ``docs/plans/music-synced-lyrics.md`` for the design.
"""

from __future__ import annotations

import pytest

from poob.music.lyrics import LyricsResolver, ParsedLyrics, _parse_lrc


# ---------------------------------------------------------------------------
# LRC parsing
# ---------------------------------------------------------------------------

class TestParseLRC:
    def test_parse_lrc_single_line(self) -> None:
        assert _parse_lrc("[00:12.34]Hello world") == [(12.34, "Hello world")]

    def test_parse_lrc_multiple_lines_sorted(self) -> None:
        text = "[00:20.00]B\n[00:10.00]A\n[00:05.00]C"
        result = _parse_lrc(text)
        assert result == [(5.0, "C"), (10.0, "A"), (20.0, "B")]

    def test_parse_lrc_handles_mm_ss_only(self) -> None:
        # No centiseconds — still parses.
        assert _parse_lrc("[01:23]X") == [(83.0, "X")]

    def test_parse_lrc_skips_malformed_lines(self) -> None:
        text = (
            "[00:10.00]Good\n"
            "garbage line with no timestamp\n"
            "[00:20.00]Also good\n"
            "[malformed]bad\n"
            "[00:30.00]Final"
        )
        result = _parse_lrc(text)
        assert result == [(10.0, "Good"), (20.0, "Also good"), (30.0, "Final")]

    def test_parse_lrc_preserves_instrumental_breaks(self) -> None:
        # Empty body after timestamp is a legitimate LRC convention.
        text = "[00:10.00]Verse\n[00:25.00]\n[00:30.00]Chorus"
        result = _parse_lrc(text)
        assert (25.0, "") in result


# ---------------------------------------------------------------------------
# ParsedLyrics.current_line_at + upcoming_window
# ---------------------------------------------------------------------------

class TestCurrentLineAt:
    def _make_lyrics(self) -> ParsedLyrics:
        return ParsedLyrics(
            title="T", artist="A", is_synced=True,
            lines=[(10.0, "A"), (20.0, "B"), (30.0, "C")],
        )

    def test_picks_last_passed_timestamp(self) -> None:
        lyrics = self._make_lyrics()
        assert lyrics.current_line_at(15.0) == "A"
        assert lyrics.current_line_at(25.0) == "B"
        assert lyrics.current_line_at(40.0) == "C"

    def test_before_first_returns_none(self) -> None:
        lyrics = self._make_lyrics()
        assert lyrics.current_line_at(5.0) is None
        assert lyrics.current_line_at(0.0) is None

    def test_exact_timestamp_match_picks_that_line(self) -> None:
        lyrics = self._make_lyrics()
        assert lyrics.current_line_at(10.0) == "A"
        assert lyrics.current_line_at(20.0) == "B"


class TestUpcomingWindow:
    def test_upcoming_window_returns_context(self) -> None:
        lyrics = ParsedLyrics(
            title="T", artist="A", is_synced=True,
            lines=[
                (10.0, "L1"), (20.0, "L2"), (30.0, "L3"),
                (40.0, "L4"), (50.0, "L5"),
            ],
        )
        # At position 25 → current is L2 (idx 1). With before=1 / after=3 →
        # L1 (idx 0) + L2 (current) + L3 + L4 + L5 = 5 entries
        window = lyrics.upcoming_window(25.0, before=1, after=3)
        assert window == [
            (False, "L1"),
            (True, "L2"),
            (False, "L3"),
            (False, "L4"),
            (False, "L5"),
        ]

    def test_upcoming_window_before_first_line(self) -> None:
        lyrics = ParsedLyrics(
            title="T", artist="A", is_synced=True,
            lines=[(10.0, "L1"), (20.0, "L2"), (30.0, "L3")],
        )
        # Before any line fires — none are current, show the first batch
        window = lyrics.upcoming_window(5.0, before=1, after=3)
        assert all(not is_current for is_current, _text in window)
        assert window[0][1] == "L1"

    def test_upcoming_window_empty_lyrics_returns_empty(self) -> None:
        lyrics = ParsedLyrics(title="T", artist="A", is_synced=True, lines=[])
        assert lyrics.upcoming_window(0.0) == []


# ---------------------------------------------------------------------------
# Resolver cascade — LRCLIB synced -> plain -> None
# ---------------------------------------------------------------------------

class TestResolverCascade:
    async def test_returns_synced_when_lrclib_has_synced(self) -> None:
        # Mock returns LRC text on the synced call.
        def fake_search(query, providers, plain_only):
            if not plain_only:
                return "[00:05.00]First line\n[00:15.00]Second line"
            return None

        resolver = LyricsResolver(_search_func=fake_search)
        result = await resolver.fetch("Test Track", "Test Artist")
        assert result is not None
        assert result.is_synced is True
        assert result.title == "Test Track"
        assert result.artist == "Test Artist"
        assert len(result.lines) == 2
        assert result.lines[0] == (5.0, "First line")

    async def test_falls_back_to_plain(self) -> None:
        # Synced call returns None; plain call returns text.
        def fake_search(query, providers, plain_only):
            if plain_only:
                return "Plain text lyrics body\nLine 2\nLine 3"
            return None

        resolver = LyricsResolver(_search_func=fake_search)
        result = await resolver.fetch("Test Track", "Test Artist")
        assert result is not None
        assert result.is_synced is False
        assert len(result.lines) == 1
        assert result.lines[0][0] == 0.0
        assert "Plain text lyrics" in result.lines[0][1]

    async def test_returns_none_when_no_lyrics_anywhere(self) -> None:
        def fake_search(query, providers, plain_only):
            return None

        resolver = LyricsResolver(_search_func=fake_search)
        result = await resolver.fetch("Obscure", "Artist")
        assert result is None

    async def test_swallows_exceptions_returns_none(self) -> None:
        def fake_search(query, providers, plain_only):
            raise RuntimeError("network error")

        resolver = LyricsResolver(_search_func=fake_search)
        result = await resolver.fetch("X", "Y")
        assert result is None

    async def test_empty_title_returns_none_without_calling_search(self) -> None:
        called = {"hit": False}

        def fake_search(query, providers, plain_only):
            called["hit"] = True
            return None

        resolver = LyricsResolver(_search_func=fake_search)
        result = await resolver.fetch("", "Artist")
        assert result is None
        assert called["hit"] is False
