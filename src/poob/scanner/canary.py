"""Canary listing detection — ground truth for "are we seeing every listing?"

The problem: Facebook Marketplace is officially a personalized, ranked feed
(Meta's own transparency docs admit the feed is AI-ranked, not chronological).
We can't know whether our scraper is catching every fresh listing in the
user's area, or only a personalized subset, without an external signal.

The solution: the user posts a dummy listing from a second FB account with
a known magic string in the title. We watch every listing we collect for
that string and record:

- **time-to-detection**: seconds from canary expected-post-time to our first
  observation. This is the concrete SLA for "just listed" detection.
- **collection path**: which sweep saw it first (anonymous GQL vs anonymous
  DOM vs watchlist sweep) — tells us which layers are doing useful work.
- **missed canaries**: if a canary is still unseen N hours after its
  expected-post-time, we flag it as a MISS. Real degradation signal.

Canaries are registered via :meth:`CanaryRegistry.register` (from an admin
command / DM / config) and scanned by :meth:`check_batch` on every sweep.
This module is intentionally in-memory only — canaries have a 24h lifetime
and persistence through restarts isn't worth the SQLite schema churn yet.

Canary posting workflow (outside this module):
    1. Operator posts a FB Marketplace listing from a burner account with
       a title like ``"[Wood Lamp POOB-CANARY-a1b2c3d4] vintage lamp"``.
    2. Operator calls a bot command that invokes
       :meth:`CanaryRegistry.register` with the token ``"POOB-CANARY-a1b2c3d4"``
       and the expected posting time.
    3. Next patrol cycle observes it, this module logs ``canary.detected``
       with latency. If 4 cycles pass without detection, logs ``canary.missed``.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from poob.storage.models import Listing
from poob.utils.logging import get_logger

log = get_logger("scanner.canary")

# Canaries are identified by this prefix anywhere in the title or description.
# Format: POOB-CANARY-{8+ hex chars} — case-insensitive match.
CANARY_TOKEN_RE = re.compile(r"POOB-CANARY-[A-Fa-f0-9]{4,}")

# If no detection within this many minutes of the expected-post-time, log
# a miss. Two patrol cycles at peak cadence (3 min) * safety factor.
_DEFAULT_MISS_THRESHOLD_MIN = 20.0

# Purge entries older than this. Covers a full slow off-peak cycle plus
# ample debug headroom.
_DEFAULT_TTL_HOURS = 24.0


@dataclass
class CanaryEntry:
    """One active canary being watched for."""

    token: str  # Upper-cased form, e.g. "POOB-CANARY-A1B2C3D4"
    expected_at: datetime  # Operator-provided; when they hit "post" in FB
    registered_at: datetime
    label: str = ""  # Optional human description ("night test #3")
    detected_at: datetime | None = None
    detected_source: str = ""  # Which sweep path saw it first
    detected_listing_id: str = ""
    miss_logged: bool = False  # So we don't spam "missed" on every cycle


@dataclass
class CanaryDetection:
    """Returned to the caller when a listing matches an active canary."""

    token: str
    latency_seconds: float  # detected_at - expected_at
    source: str
    listing: Listing


class CanaryRegistry:
    """Thread-safe registry of active canary tokens.

    One instance lives on the :class:`PatrolEngine`; the patrol loop calls
    :meth:`check_batch` after dedup, before filtering. Filters run afterwards
    — we want the raw detection signal, not the filtered one.
    """

    def __init__(
        self,
        *,
        miss_threshold_minutes: float = _DEFAULT_MISS_THRESHOLD_MIN,
        ttl_hours: float = _DEFAULT_TTL_HOURS,
    ) -> None:
        self._entries: dict[str, CanaryEntry] = {}
        self._lock = threading.Lock()
        self._miss_threshold = timedelta(minutes=miss_threshold_minutes)
        self._ttl = timedelta(hours=ttl_hours)

    def register(
        self,
        token: str,
        *,
        expected_at: datetime | None = None,
        label: str = "",
    ) -> CanaryEntry:
        """Register a new active canary.

        Args:
            token: The magic-string token (case-insensitive). Must match
                :data:`CANARY_TOKEN_RE`. Stored upper-case internally.
            expected_at: When the operator posted the listing on FB. If
                ``None``, uses ``datetime.now(UTC)`` (the "I just posted it"
                path). Caller should pass explicit UTC for out-of-band
                registration.
            label: Optional free-form description for log readability.

        Returns:
            The stored :class:`CanaryEntry`.
        """
        normalized = token.upper().strip()
        if not CANARY_TOKEN_RE.fullmatch(normalized):
            raise ValueError(
                f"Invalid canary token format: {token!r} — expected POOB-CANARY-HEX",
            )
        now = datetime.now(timezone.utc)
        entry = CanaryEntry(
            token=normalized,
            expected_at=expected_at or now,
            registered_at=now,
            label=label,
        )
        with self._lock:
            self._entries[normalized] = entry
        log.info(
            "canary.registered",
            token=normalized,
            expected_at=entry.expected_at.isoformat(),
            label=label,
        )
        return entry

    def deregister(self, token: str) -> bool:
        """Remove a canary. Returns True if it existed."""
        normalized = token.upper().strip()
        with self._lock:
            removed = self._entries.pop(normalized, None)
        if removed is not None:
            log.info("canary.deregistered", token=normalized)
            return True
        return False

    def active(self) -> list[CanaryEntry]:
        """Snapshot of currently-active canaries (for status commands)."""
        with self._lock:
            return list(self._entries.values())

    def check_batch(
        self, listings: list[Listing], *, source: str,
    ) -> list[CanaryDetection]:
        """Scan a batch of listings for any active canary token.

        Emits ``canary.detected`` log events for first-time matches and
        ``canary.missed`` events when an active canary has exceeded its
        miss threshold (logged once per canary).

        Args:
            listings: The batch to check. Safe to pass the raw sweep result
                — this is lightweight (regex over title + description).
            source: Collection path label ("anonymous_graphql",
                "anonymous_dom", "watchlist_sweep", "backlog").

        Returns:
            Detections for canaries seen for the first time in this call.
        """
        if not listings:
            self._check_misses()
            return []

        detections: list[CanaryDetection] = []
        now = datetime.now(timezone.utc)

        with self._lock:
            active_tokens = {k: v for k, v in self._entries.items() if v.detected_at is None}

        for listing in listings:
            if not active_tokens:
                break
            haystack = f"{listing.title or ''} {listing.description or ''}".upper()
            for match in CANARY_TOKEN_RE.finditer(haystack):
                token = match.group(0)
                entry = active_tokens.get(token)
                if entry is None:
                    continue
                latency = (now - entry.expected_at).total_seconds()
                entry.detected_at = now
                entry.detected_source = source
                entry.detected_listing_id = listing.external_id or listing.id or ""
                with self._lock:
                    self._entries[token] = entry
                active_tokens.pop(token, None)
                detections.append(
                    CanaryDetection(
                        token=token,
                        latency_seconds=latency,
                        source=source,
                        listing=listing,
                    )
                )
                log.info(
                    "canary.detected",
                    token=token,
                    latency_seconds=round(latency, 1),
                    source=source,
                    listing_id=listing.external_id,
                    listing_title=(listing.title or "")[:80],
                    label=entry.label,
                )

        self._check_misses()
        self._purge_stale(now)
        return detections

    def _check_misses(self) -> None:
        """Log ``canary.missed`` for any active canary past miss threshold."""
        now = datetime.now(timezone.utc)
        with self._lock:
            entries = list(self._entries.items())
        for token, entry in entries:
            if entry.detected_at is not None or entry.miss_logged:
                continue
            age = now - entry.expected_at
            if age < self._miss_threshold:
                continue
            entry.miss_logged = True
            with self._lock:
                self._entries[token] = entry
            log.warning(
                "canary.missed",
                token=token,
                age_minutes=round(age.total_seconds() / 60.0, 1),
                miss_threshold_minutes=round(self._miss_threshold.total_seconds() / 60.0, 1),
                label=entry.label,
            )

    def _purge_stale(self, now: datetime) -> None:
        """Drop entries older than the TTL — success or miss, doesn't matter."""
        with self._lock:
            to_delete = [
                token
                for token, entry in self._entries.items()
                if now - entry.registered_at > self._ttl
            ]
            for token in to_delete:
                del self._entries[token]
        if to_delete:
            log.debug("canary.purged_stale", count=len(to_delete))
