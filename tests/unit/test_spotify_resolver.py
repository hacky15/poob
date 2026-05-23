"""Tests for the Spotify public-playlist URL resolver.

Mocks ``spotipy.Spotify`` end-to-end so the suite runs without the
package installed and without any network. See
``docs/plans/music-spotify-playlist-import.md`` for the design.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from poob.music.spotify import (
    SpotifyPlaylistResolver,
    parse_playlist_id,
)


# ---------------------------------------------------------------------------
# URL parsing
# ---------------------------------------------------------------------------

class TestParsePlaylistId:
    def test_parse_playlist_id_from_web_url(self) -> None:
        assert (
            parse_playlist_id("https://open.spotify.com/playlist/abc123XYZ")
            == "abc123XYZ"
        )

    def test_parse_playlist_id_from_uri(self) -> None:
        assert parse_playlist_id("spotify:playlist:abc123XYZ") == "abc123XYZ"

    def test_parse_playlist_id_strips_query_string(self) -> None:
        assert (
            parse_playlist_id(
                "https://open.spotify.com/playlist/abc123XYZ?si=foo&utm=bar",
            )
            == "abc123XYZ"
        )

    def test_parse_invalid_url_returns_none(self) -> None:
        assert parse_playlist_id("https://youtube.com/watch?v=foo") is None
        assert parse_playlist_id("not a url") is None
        assert parse_playlist_id("") is None


# ---------------------------------------------------------------------------
# Configuration gating
# ---------------------------------------------------------------------------

class TestIsConfigured:
    def test_is_configured_requires_both_secrets(self) -> None:
        assert SpotifyPlaylistResolver("", "").is_configured() is False
        assert SpotifyPlaylistResolver("id", "").is_configured() is False
        assert SpotifyPlaylistResolver("", "secret").is_configured() is False
        assert SpotifyPlaylistResolver("id", "secret").is_configured() is True


# ---------------------------------------------------------------------------
# Resolve path with mocked Spotipy client
# ---------------------------------------------------------------------------

def _make_client_with_pages(pages: list[dict]):
    """Build a mock Spotipy client that returns ``pages`` sequentially.

    First call to ``playlist_items`` returns ``pages[0]``; subsequent
    calls to ``next(page)`` return the remaining pages in order.
    """
    client = MagicMock()
    iterator = iter(pages)
    client.playlist_items.return_value = next(iterator)

    def _next_side_effect(_page):
        return next(iterator, None)

    client.next.side_effect = _next_side_effect
    return client


class TestResolveHappyPath:
    async def test_resolve_returns_title_artist_list(self) -> None:
        page = {
            "items": [
                {"track": {"name": "Track One", "artists": [{"name": "Artist A"}]}},
                {"track": {"name": "Track Two", "artists": [{"name": "Artist B"}]}},
                {"track": {"name": "Track Three", "artists": [{"name": "Artist C"}]}},
            ],
            "next": None,
        }
        client = _make_client_with_pages([page])
        resolver = SpotifyPlaylistResolver(
            "id", "secret", _client_factory=lambda: client,
        )

        result = await resolver.resolve(
            "https://open.spotify.com/playlist/playlist123",
        )

        assert result == [
            {"title": "Track One", "artist": "Artist A"},
            {"title": "Track Two", "artist": "Artist B"},
            {"title": "Track Three", "artist": "Artist C"},
        ]

    async def test_resolve_pages_through_next(self) -> None:
        page1 = {
            "items": [
                {"track": {"name": "T1", "artists": [{"name": "A1"}]}},
            ],
            "next": "https://api.spotify.com/v1/playlists/foo/tracks?offset=100",
        }
        page2 = {
            "items": [
                {"track": {"name": "T2", "artists": [{"name": "A2"}]}},
            ],
            "next": None,
        }
        client = _make_client_with_pages([page1, page2])
        resolver = SpotifyPlaylistResolver(
            "id", "secret", _client_factory=lambda: client,
        )

        result = await resolver.resolve("spotify:playlist:playlist123")

        assert len(result) == 2
        assert result[0]["title"] == "T1"
        assert result[1]["title"] == "T2"
        assert client.next.called

    async def test_resolve_skips_null_track_items(self) -> None:
        page = {
            "items": [
                {"track": {"name": "Good Track", "artists": [{"name": "A"}]}},
                {"track": None},  # unavailable / pulled from catalog
                {"track": {"name": "", "artists": [{"name": "A"}]}},  # empty title
                {"track": {"name": "Another Good", "artists": [{"name": "B"}]}},
            ],
            "next": None,
        }
        client = _make_client_with_pages([page])
        resolver = SpotifyPlaylistResolver(
            "id", "secret", _client_factory=lambda: client,
        )

        result = await resolver.resolve("spotify:playlist:p1")
        assert len(result) == 2
        assert result[0]["title"] == "Good Track"
        assert result[1]["title"] == "Another Good"


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------

class TestResolveFailures:
    async def test_resolve_handles_api_error_returns_none(self) -> None:
        client = MagicMock()
        client.playlist_items.side_effect = RuntimeError("rate limited")
        resolver = SpotifyPlaylistResolver(
            "id", "secret", _client_factory=lambda: client,
        )

        result = await resolver.resolve("spotify:playlist:p1")
        assert result is None

    async def test_resolve_unparseable_url_returns_none(self) -> None:
        resolver = SpotifyPlaylistResolver("id", "secret")
        assert (
            await resolver.resolve("https://youtube.com/playlist?list=foo")
            is None
        )

    async def test_resolve_without_credentials_returns_none(self) -> None:
        resolver = SpotifyPlaylistResolver("", "")
        assert (
            await resolver.resolve("https://open.spotify.com/playlist/p1")
            is None
        )
