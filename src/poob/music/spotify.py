"""Spotify public-playlist URL resolver.

Translates a Spotify playlist URL into a list of ``{title, artist}``
dicts via Spotipy's ``SpotifyClientCredentials`` flow. Read-only, no
OAuth, no Spotify Premium — works for any public playlist. The
``/recommendations`` deprecation from Feb 2026 didn't touch
``playlist_items``; the playlist-metadata reads still operate on the
free tier.

This module is intentionally narrow: parse a URL, fetch the playlist
items (paging through ``next`` for big playlists), return clean title +
artist dicts. The downstream resolution to playable tracks (YT search,
etc.) lives in the music cog's dispatch path so this module stays
vendor-agnostic and easy to mock in tests.
"""

from __future__ import annotations

import asyncio
import re

from poob.utils.logging import get_logger

log = get_logger("music.spotify")


# Two URL shapes Spotify ships:
#   https://open.spotify.com/playlist/<id>[?si=...]
#   https://open.spotify.com/intl-<lang>/playlist/<id>[?si=...]  (shared links
#     since ~2023 carry a locale segment — e.g. intl-de, intl-pt-br)
#   spotify:playlist:<id>
# We accept all. Anything else returns None at parse time.
_PLAYLIST_URL_RE = re.compile(
    r"(?:https?://open\.spotify\.com/(?:intl-[a-z-]+/)?playlist/|spotify:playlist:)"
    r"(?P<id>[A-Za-z0-9]+)",
)

# Single-track links — same shapes (incl. the intl- locale segment). Users
# paste these in chat with "play this"; tracks can't be streamed (DRM) but
# their metadata resolves to a title+artist we can YT-search. See
# docs/incidents/spotify-track-link-play-dead-end.md.
_TRACK_URL_RE = re.compile(
    r"(?:https?://open\.spotify\.com/(?:intl-[a-z-]+/)?track/|spotify:track:)"
    r"(?P<id>[A-Za-z0-9]+)",
)


def parse_playlist_id(url: str) -> str | None:
    """Extract the playlist ID from a Spotify URL.

    Returns ``None`` if ``url`` isn't a recognized Spotify playlist URL.
    """
    if not url:
        return None
    match = _PLAYLIST_URL_RE.search(url)
    if match is None:
        return None
    return match.group("id")


def parse_track_id(url: str) -> str | None:
    """Extract the track ID from a Spotify single-track URL.

    Returns ``None`` if ``url`` isn't a recognized Spotify track URL.
    """
    if not url:
        return None
    match = _TRACK_URL_RE.search(url)
    if match is None:
        return None
    return match.group("id")


class SpotifyPlaylistResolver:
    """Public-playlist URL → ``[{title, artist}]`` resolver.

    Lazily constructs the Spotipy client on first use so importing this
    module is cheap and tests can substitute their own fakes via the
    ``_client_factory`` constructor hook.
    """

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        _client_factory=None,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._client = None
        # Hook for tests — replaces the real spotipy.Spotify construction.
        # Production passes None; resolver constructs the real client lazily.
        self._client_factory = _client_factory

    def is_configured(self) -> bool:
        """Return True iff both credentials are non-empty."""
        return bool(self._client_id) and bool(self._client_secret)

    def _get_client(self):
        if self._client is not None:
            return self._client
        if self._client_factory is not None:
            self._client = self._client_factory()
            return self._client
        # Real-deal construction — lazy-import to keep test runs from
        # requiring spotipy if no one calls into this path.
        from spotipy import Spotify
        from spotipy.oauth2 import SpotifyClientCredentials

        auth = SpotifyClientCredentials(
            client_id=self._client_id,
            client_secret=self._client_secret,
        )
        self._client = Spotify(auth_manager=auth)
        return self._client

    async def resolve(self, url: str) -> list[dict] | None:
        """Resolve a Spotify playlist URL to ``[{title, artist}, ...]``.

        Returns ``None`` if the URL is unparseable, the credentials are
        missing, or the Spotify API raises. Every failure is logged at
        WARN with the message trimmed.

        Pages through ``playlist_items``' ``next`` URLs so big playlists
        (>100 tracks) come back whole, not just the first page.
        """
        if not self.is_configured():
            log.warning("spotify.resolve called without credentials")
            return None

        playlist_id = parse_playlist_id(url)
        if playlist_id is None:
            return None

        try:
            return await asyncio.to_thread(self._fetch_items, playlist_id)
        except Exception as exc:
            log.warning("spotify.resolve failed", error=str(exc)[:120])
            return None

    async def resolve_track(self, url: str) -> dict[str, str] | None:
        """Resolve a Spotify single-track URL to ``{title, artist}``.

        Returns ``None`` if the URL is unparseable, the credentials are
        missing, or the Spotify API raises — same contract as ``resolve``.
        """
        if not self.is_configured():
            log.warning("spotify.resolve_track called without credentials")
            return None

        track_id = parse_track_id(url)
        if track_id is None:
            return None

        try:
            return await asyncio.to_thread(self._fetch_track, track_id)
        except Exception as exc:
            log.warning("spotify.resolve_track failed", error=str(exc)[:120])
            return None

    def _fetch_track(self, track_id: str) -> dict[str, str] | None:
        """Synchronous single-track fetcher — runs under ``asyncio.to_thread``."""
        client = self._get_client()
        track = client.track(track_id)
        if not track:
            return None
        title = track.get("name")
        if not title:
            return None
        artists = track.get("artists") or []
        artist_name = artists[0].get("name") if artists else ""
        return {"title": title, "artist": artist_name or ""}

    def _fetch_items(self, playlist_id: str) -> list[dict]:
        """Synchronous fetcher — runs under ``asyncio.to_thread``.

        Pages through Spotify's ``next`` URL convention until exhausted.
        Skips null ``track`` items (Spotify includes them for tracks
        that have been pulled from the catalog).
        """
        client = self._get_client()
        results: list[dict] = []
        page = client.playlist_items(
            playlist_id,
            fields="items.track(name,artists(name)),next",
        )
        while page is not None:
            for item in page.get("items", []) or []:
                track = item.get("track")
                if track is None:
                    continue
                title = track.get("name")
                if not title:
                    continue
                artists = track.get("artists") or []
                artist_name = artists[0].get("name") if artists else ""
                results.append({"title": title, "artist": artist_name or ""})
            next_url = page.get("next")
            if not next_url:
                break
            page = client.next(page)
        return results
