"""Collection observability — measures whether the patrol is actually seeing
fresh listings or being fed stale/personalized content by Facebook.

Emits structured log events per sweep so an operator can answer three
questions from `scripts/logs.sh`:

1. "Am I seeing listings right after they post?" — ``collection.age_histogram``
   shows the distribution of listing age at the moment we first observed it.
   A healthy pipeline has mass in the 0-5 minute bucket.
2. "How many listings slip past with no creation_time?" — ``no_timestamp``
   count per sweep. Facebook's anonymous browse endpoint sometimes omits
   ``creation_time``; high counts here mean our freshness filter is running
   blind on a meaningful fraction of the feed.
3. "Is Facebook ignoring daysSinceListed=1?" — ``fb_filter_violations`` count
   of listings older than the requested ``daysSinceListed * 24h`` window.
   This is the direct evidence of FB injecting stale padding.

Everything here is read-only — it observes the listing stream, it doesn't
filter it. Filtering stays in :mod:`poob.scanner.listing_filter`.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone

from poob.storage.models import Listing
from poob.utils.logging import get_logger

log = get_logger("scanner.observability")


# Buckets in minutes, left-inclusive upper bound (0-5 means 0 <= age < 5).
# Chosen to match the product framing:
#   - "0-5m" is the "just listed" window we're targeting
#   - "5-15m" is still competitive for flipping
#   - "15-60m" is late but catchable
#   - "60m-6h" is evaluation-window but not notification-window
#   - ">6h" is past evaluation cutoff — shouldn't be here, diagnostic only
_AGE_BUCKETS_MIN: tuple[tuple[str, float | None], ...] = (
    ("0-5m", 5.0),
    ("5-15m", 15.0),
    ("15-60m", 60.0),
    ("60m-6h", 360.0),
    (">6h", None),  # None = open-ended
)


@dataclass
class SweepSnapshot:
    """Aggregate measurements for one patrol sweep.

    Not persisted — emitted as a structured log event and discarded. The
    store-of-record for historical analysis is the log aggregator (loki,
    grep of `scripts/logs.sh`, etc.).
    """

    total: int = 0
    with_timestamp: int = 0
    no_timestamp: int = 0
    age_buckets: dict[str, int] = field(
        default_factory=lambda: {name: 0 for name, _ in _AGE_BUCKETS_MIN}
    )
    fb_filter_violations: int = 0
    min_age_minutes: float | None = None
    max_age_minutes: float | None = None


def _age_minutes(listing: Listing, now: datetime) -> float | None:
    if listing.posted_at is None:
        return None
    delta = now - listing.posted_at
    return delta.total_seconds() / 60.0


def _bucket_for(age_min: float) -> str:
    for name, upper in _AGE_BUCKETS_MIN:
        if upper is None or age_min < upper:
            return name
    return ">6h"  # Defensive — the open bucket should always match


def measure_sweep(
    listings: Iterable[Listing],
    *,
    days_since_listed: int,
    now: datetime | None = None,
) -> SweepSnapshot:
    """Compute age histogram + freshness stats for a batch of listings.

    Args:
        listings: Everything we observed this sweep — pre-dedup, pre-filter.
            Pass the raw collection so we measure what FB actually served us,
            not what survived our filters.
        days_since_listed: The ``daysSinceListed`` filter value we requested
            from Facebook. Used to count ``fb_filter_violations`` — listings
            older than this that FB served anyway.
        now: Override for testing. Defaults to ``datetime.now(timezone.utc)``.

    Returns:
        Populated :class:`SweepSnapshot`. Caller is expected to emit it via
        :func:`log_sweep`.
    """
    now = now or datetime.now(timezone.utc)
    violation_cutoff_min = days_since_listed * 24 * 60

    snap = SweepSnapshot()

    for listing in listings:
        snap.total += 1
        age_min = _age_minutes(listing, now)

        if age_min is None:
            snap.no_timestamp += 1
            continue

        snap.with_timestamp += 1
        snap.age_buckets[_bucket_for(age_min)] += 1

        if snap.min_age_minutes is None or age_min < snap.min_age_minutes:
            snap.min_age_minutes = age_min
        if snap.max_age_minutes is None or age_min > snap.max_age_minutes:
            snap.max_age_minutes = age_min

        if age_min > violation_cutoff_min:
            snap.fb_filter_violations += 1

    return snap


def log_sweep(snap: SweepSnapshot, *, source: str) -> None:
    """Emit a structured ``collection.sweep_metrics`` event.

    Args:
        snap: Snapshot from :func:`measure_sweep`.
        source: Label for the collection path (``"anonymous_graphql"``,
            ``"anonymous_dom"``, ``"watchlist_sweep"``, etc.). Makes it
            possible to compare which paths pull the freshest data.
    """
    log.info(
        "collection.sweep_metrics",
        source=source,
        total=snap.total,
        with_timestamp=snap.with_timestamp,
        no_timestamp=snap.no_timestamp,
        fb_filter_violations=snap.fb_filter_violations,
        min_age_min=(
            round(snap.min_age_minutes, 1) if snap.min_age_minutes is not None else None
        ),
        max_age_min=(
            round(snap.max_age_minutes, 1) if snap.max_age_minutes is not None else None
        ),
        **{f"bucket_{name}": count for name, count in snap.age_buckets.items()},
    )
