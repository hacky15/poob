"""Unified listing filter pipeline.

Replaces the fragmented triple-check pattern (pre-enrichment + post-enrichment
+ notification safety net) with a single composable filter chain. Each filter
is a frozen dataclass with a ``__call__`` method returning a ``FilterVerdict``.
The ``FilterChain`` orchestrates execution, respects per-listing tag-based
exemptions, and handles logging.

Architecture notes
------------------
- Filters are **synchronous** predicates — no I/O, just field checks.
- The pipeline that calls them is async, but the filters themselves are not.
- Filters declare which ``FilterStage`` they belong to so the chain only
  runs applicable filters at each pipeline step.
- Tag-based exemptions decouple watchlist logic from filter logic:
  upstream code computes tags, filters declare which tags exempt them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum, auto

import structlog

from poob.storage.models import Listing
from poob.utils.geo import (
    has_valid_coordinates,
    haversine_miles,
    parse_state_from_location,
    state_centroid_distance,
)

log = structlog.get_logger()


# ---------------------------------------------------------------------------
# Enums & verdict types
# ---------------------------------------------------------------------------

class FilterStage(Enum):
    """When in the pipeline a filter can run."""

    PRE_ENRICHMENT = auto()   # Only needs basic listing fields from search
    POST_ENRICHMENT = auto()  # Needs description, coordinates, posted_at from detail pages


@dataclass(frozen=True, slots=True)
class FilterVerdict:
    """Result of a single filter check on one listing."""

    passed: bool
    filter_name: str
    reason: str = ""

    @staticmethod
    def ok(name: str) -> FilterVerdict:
        return FilterVerdict(passed=True, filter_name=name)

    @staticmethod
    def reject(name: str, reason: str) -> FilterVerdict:
        return FilterVerdict(passed=False, filter_name=name, reason=reason)

    @staticmethod
    def skip(name: str) -> FilterVerdict:
        """Data missing — treated as pass (benefit of the doubt)."""
        return FilterVerdict(passed=True, filter_name=name, reason="skipped:missing_data")


@dataclass
class FilterResult:
    """Aggregate result of running all applicable filters on one listing."""

    listing_id: str
    passed: bool = True
    verdicts: list[FilterVerdict] = field(default_factory=list)

    @property
    def rejections(self) -> list[FilterVerdict]:
        return [v for v in self.verdicts if not v.passed]

    @property
    def rejection_summary(self) -> str:
        return "; ".join(f"{v.filter_name}: {v.reason}" for v in self.rejections)


# ---------------------------------------------------------------------------
# Individual filters
# ---------------------------------------------------------------------------

# Facebook Marketplace category IDs for vehicles and housing.
# These are the ``marketplace_listing_category_id`` values Facebook assigns
# to listings in these categories. Extracted from GraphQL search responses.
# When a listing has a category_id, this is 100% reliable — no keyword
# matching needed. Falls back to keyword matching when category_id is absent.
#
# Sources:
# - docs/research/fb-graphql-schema.md (confirmed in response schema)
# - Meta Commerce Catalog API vehicle_type enum
# - Observed from live GraphQL responses (will be validated on next run)
#
# NOTE: These IDs need empirical verification. The filter gracefully falls
# back to keyword matching if category_id is missing or unrecognized.
EXCLUDED_CATEGORY_IDS: dict[str, set[str]] = {
    "vehicles": {
        # Known Facebook vehicle category IDs — to be populated from live data.
        # The debug key dump (Phase 2A) will reveal the exact values.
    },
    "housing": set(),
}

# Simplified keyword fallback — used ONLY when marketplace_listing_category_id
# is absent. Much smaller than the old 65+ keyword set because:
# 1. Category ID filtering handles the majority of cases
# 2. Only high-signal keywords that are unambiguous vehicle/housing indicators
_VEHICLE_KEYWORDS: frozenset[str] = frozenset({
    # Vehicle body types (unambiguous)
    "car", "truck", "suv", "sedan", "coupe", "convertible", "minivan",
    "motorcycle", "motorbike", "dirt bike", "atv", "quad",
    "rv ", " rv", "motorhome", "camper van",
    # Vehicle-specific terms (never appear in non-vehicle listings)
    "salvage title", "clean title", "rebuilt title",
    "odometer", "mileage", "vin ", "4wd", "4x4",
    "crew cab", "extended cab",
})

_HOUSING_KEYWORDS: frozenset[str] = frozenset({
    "house for rent", "house for sale", "condo for",
    "apartment", "duplex", "townhouse", "townhome",
    "bedroom apt", "br apt", "sqft home", "sq ft home",
    "lease takeover", "sublease", "sublet",
    "/month rent", "/mo rent", "rent per month",
})

_COUCH_KEYWORDS: frozenset[str] = frozenset({
    "couch", "sofa", "sectional", "loveseat", "futon",
    "sleeper sofa", "recliner sofa", "chaise lounge",
})

# All keyword sets merged for O(1) lookup.
_ALL_EXCLUDED_KEYWORDS: frozenset[str] = (
    _VEHICLE_KEYWORDS | _HOUSING_KEYWORDS | _COUCH_KEYWORDS
)

# Garbage title patterns.
_GARBAGE_TITLES: frozenset[str] = frozenset({
    "see details", "more options", "seller details", "loading",
    "marketplace", "facebook marketplace",
})

_UNENRICHABLE_TITLES: frozenset[str] = frozenset({
    "just listed", "listed today", "listed yesterday", "new listing", "",
})

_GARBAGE_LOCATIONS: frozenset[str] = frozenset({
    "more options", "seller details", "see details",
})

_LISTED_AGO_RE = re.compile(r"listed\s+\d+\s*[hmd]\w*\s+ago")

# Sale-EVENTS and curb-alerts are not items — they have no single resaleable
# product, yet the VLM scores them (and prompts.py used to reward them as
# "urgency"). Reject deterministically. Word-boundary matched so legit titles
# ("Large sectional for sale", "Free treadmill at the curb", "Tag heuer watch",
# "Curb your enthusiasm DVD") are untouched. The ambiguous "moving sale" /
# "multi-family sale" / "neighborhood sale" are DELIBERATELY excluded — they
# attach to genuine single-item titles and could suppress a real watchlist DM.
# See docs/decisions/public-incredible-selectivity-floors.md.
_SALE_EVENT_PHRASES: frozenset[str] = frozenset({
    "rummage sale", "garage sale", "estate sale", "yard sale",
    "barn sale", "tag sale", "curb alert", "free at curb",
    "free curb", "everything must go",
})
_SALE_EVENT_RE = re.compile(
    r"\b(?:" + "|".join(p.replace(" ", r"\s+") for p in _SALE_EVENT_PHRASES) + r")\b"
)


class KnownFarLocationFilter:
    """Skip enrichment on towns that are entirely outside the search radius,
    learned from GeoDistanceFilter rejections.

    A single FB location label (e.g. "Madison, WI") can straddle the radius
    boundary — pins labeled "Madison, WI" range 0-44mi from center. A town is
    therefore cached as far ONLY while it has never been observed in-radius:
    the first confirmed in-radius pin permanently vetoes the town from the
    cache (and un-poisons it if already cached). This prevents one borderline
    pin from rejecting a boundary-straddling town's many in-radius listings.
    See docs/incidents/known-far-location-cache-poisoning.md.

    Learned cache persists within a session (resets on restart).
    """

    name: str = "known_far_location"
    stage: FilterStage = FilterStage.PRE_ENRICHMENT
    exempt_tags: frozenset[str] = frozenset({"watchlist_geo_override"})

    def __init__(
        self,
        known_far: set[str] | None = None,
        known_near: set[str] | None = None,
    ) -> None:
        self._known_far = known_far if known_far is not None else set()
        self._known_near = known_near if known_near is not None else set()

    def learn_far(self, location: str) -> None:
        """Record a location whose pin GeoDistanceFilter rejected as
        out-of-radius. No-op once the string has been observed in-radius."""
        loc = location.strip().lower()
        if loc and loc not in self._known_near:
            self._known_far.add(loc)

    def learn_near(self, location: str) -> None:
        """Record a confirmed in-radius pin for this location string. Vetoes
        the string from the far-cache permanently and un-poisons it if already
        cached — a boundary-straddling town must never be skipped wholesale."""
        loc = location.strip().lower()
        if loc:
            self._known_near.add(loc)
            self._known_far.discard(loc)

    def __call__(self, listing: Listing) -> FilterVerdict:
        loc = (listing.location or "").strip().lower()
        if not loc:
            return FilterVerdict.skip(self.name)
        if loc in self._known_far and loc not in self._known_near:
            return FilterVerdict.reject(
                self.name,
                f"location entirely out of radius (no in-radius pin seen): '{listing.location}'",
            )
        return FilterVerdict.ok(self.name)


@dataclass(frozen=True, slots=True)
class LocationTextFilter:
    """Coarse pre-enrichment geo filter using state centroids.

    Parses "City, STATE" from location text and checks if the state's
    centroid is impossibly far from the user's configured center point.
    This catches California/Texas/Florida listings BEFORE spending
    4+ minutes enriching them via detail pages.

    Uses a generous margin (3x radius, minimum 200 miles) to avoid
    false positives — nearby states always pass. The post-enrichment
    GeoDistanceFilter does the precise check with real coordinates.

    Watchlist-matched listings are exempt: their GQL search was already
    targeted to the user's configured location, so rejecting them based
    on the global patrol center would break multi-user watchlists.
    """

    name: str = "location_text"
    stage: FilterStage = FilterStage.PRE_ENRICHMENT
    exempt_tags: frozenset[str] = frozenset({"watchlist_geo_override"})
    center_lat: float = 0.0
    center_lon: float = 0.0
    radius_miles: float = 40.0

    def __call__(self, listing: Listing) -> FilterVerdict:
        if not has_valid_coordinates(self.center_lat, self.center_lon):
            return FilterVerdict.skip(self.name)

        state = parse_state_from_location(listing.location or "")
        if state is None:
            return FilterVerdict.skip(self.name)  # Unparseable — benefit of the doubt

        dist = state_centroid_distance(state, self.center_lat, self.center_lon)
        if dist is None:
            return FilterVerdict.skip(self.name)

        # Threshold: 3x configured radius, minimum 150 miles.
        # State centroids are ~50-100mi from the nearest border, so 150mi
        # catches states where even the closest edge is beyond reasonable
        # range, while keeping genuine border states (MN, IA for WI user).
        threshold = max(self.radius_miles * 3, 150.0)
        if dist > threshold:
            return FilterVerdict.reject(
                self.name,
                f"state={state} centroid {dist:.0f}mi away (threshold={threshold:.0f}mi)",
            )
        return FilterVerdict.ok(self.name)


@dataclass(frozen=True, slots=True)
class SponsoredFilter:
    """Reject sponsored/boosted and shipping-only listings."""

    name: str = "sponsored"
    stage: FilterStage = FilterStage.PRE_ENRICHMENT
    exempt_tags: frozenset[str] = frozenset()

    def __call__(self, listing: Listing) -> FilterVerdict:
        if listing.is_sponsored:
            return FilterVerdict.reject(self.name, "sponsored/boosted listing")
        loc = (listing.location or "").lower()
        if "ship" in loc and ("you" in loc or "nationwide" in loc):
            return FilterVerdict.reject(self.name, f"shipping-only: {listing.location}")
        return FilterVerdict.ok(self.name)


@dataclass(frozen=True, slots=True)
class CategoryFilter:
    """Reject listings in excluded categories (vehicles, housing, couches).

    Uses Facebook's ``marketplace_listing_category_id`` when available
    (100% reliable), falls back to keyword matching on title+description.
    """

    name: str = "category"
    stage: FilterStage = FilterStage.PRE_ENRICHMENT
    exempt_tags: frozenset[str] = frozenset({"watchlist_category_override"})

    def __call__(self, listing: Listing) -> FilterVerdict:
        # Tier 1: Category ID from GraphQL (most reliable).
        cat_id = listing.raw_data.get("category_id")
        if cat_id:
            for cat_name, ids in EXCLUDED_CATEGORY_IDS.items():
                if str(cat_id) in ids:
                    return FilterVerdict.reject(
                        self.name, f"category_id={cat_id} ({cat_name})",
                    )
            # Has a category ID that's NOT excluded — pass without keyword check.
            return FilterVerdict.ok(self.name)

        # Tier 2: Keyword fallback (only when no category_id).
        text = f"{listing.title} {listing.description}".lower()
        for kw in _ALL_EXCLUDED_KEYWORDS:
            if kw in text:
                return FilterVerdict.reject(self.name, f"keyword match: '{kw}'")
        return FilterVerdict.ok(self.name)


@dataclass(frozen=True, slots=True)
class FreshnessFilter:
    """Reject listings older than ``max_age_hours``.

    Stage-specific behavior on missing ``posted_at``:

    - PRE_ENRICHMENT: SKIP (timestamp may still be filled by detail-page
      enrichment in the next stage; don't drop yet).
    - POST_ENRICHMENT: REJECT (this is the last chance to enforce
      freshness; an unverified-age listing here can never fire a
      notification — every gate downstream requires posted_at — so
      spending VLM budget on it is waste).

    Supersedes the prior "skip both stages" behavior documented in
    docs/architecture/listing-freshness-verification.md, replaced by the
    stricter triage standard in
    docs/decisions/triage-freshness-converge-with-notify.md.
    """

    name: str = "freshness"
    stage: FilterStage = FilterStage.PRE_ENRICHMENT
    # Watchlist matches are exempt: a wishlist is about the item being AVAILABLE,
    # not "just listed", so a watched item shouldn't be dropped for being older
    # than the triage window. The user's per-item notification_threshold is the
    # sole gate. See docs/decisions/watchlist-honors-threshold-not-freshness.md.
    exempt_tags: frozenset[str] = frozenset({"watchlist_freshness_override"})
    max_age_hours: int = 6

    def __call__(self, listing: Listing) -> FilterVerdict:
        if self.max_age_hours <= 0:
            return FilterVerdict.ok(self.name)
        if listing.posted_at is None:
            if self.stage == FilterStage.POST_ENRICHMENT:
                return FilterVerdict.reject(
                    self.name,
                    "no posted_at after enrichment — cannot verify fresh",
                )
            return FilterVerdict.skip(self.name)
        now = datetime.now(timezone.utc)
        age_hours = (now - listing.posted_at).total_seconds() / 3600
        if age_hours > self.max_age_hours:
            return FilterVerdict.reject(
                self.name,
                f"{age_hours:.1f}h old (limit: {self.max_age_hours}h)",
            )
        return FilterVerdict.ok(self.name)


@dataclass(frozen=True, slots=True)
class GeoDistanceFilter:
    """Reject listings outside the configured search radius.

    Uses haversine distance from the user's configured center point(s).
    Coordinates come from ``raw_data["latitude"]`` / ``raw_data["longitude"]``,
    which are populated during detail page enrichment.

    Multi-center: when general browse sweeps more than one metro (e.g.
    Madison AND Appleton), a listing passes if it is within ``radius_miles``
    of the primary center OR any entry in ``extra_centers``. Without this, a
    Madison-centered filter would reject every Appleton listing (~100mi away)
    even though Appleton is an intended search area. See
    docs/decisions/multi-center-general-browse.md.

    Listings without valid coordinates pass through (benefit of the doubt).

    Watchlist-matched listings are exempt: their search was already
    geo-targeted by the watchlist item's own location config.
    """

    name: str = "geo_distance"
    stage: FilterStage = FilterStage.POST_ENRICHMENT
    exempt_tags: frozenset[str] = frozenset({"watchlist_geo_override"})
    center_lat: float = 0.0
    center_lon: float = 0.0
    radius_miles: float = 40.0
    # Additional accepted centers (lat, lon) for multi-metro general browse.
    # A listing within radius of ANY center passes. Empty = single-center.
    extra_centers: tuple[tuple[float, float], ...] = ()

    def __call__(self, listing: Listing) -> FilterVerdict:
        if not has_valid_coordinates(self.center_lat, self.center_lon):
            return FilterVerdict.skip(self.name)  # No center configured

        lat = listing.raw_data.get("latitude")
        lon = listing.raw_data.get("longitude")

        if not has_valid_coordinates(lat, lon):
            return FilterVerdict.skip(self.name)  # No coords on listing

        # Distance to the nearest accepted center.
        centers = [(self.center_lat, self.center_lon), *self.extra_centers]
        min_dist = min(
            haversine_miles(clat, clon, lat, lon) for clat, clon in centers
        )
        if min_dist > self.radius_miles:
            return FilterVerdict.reject(
                self.name,
                f"{min_dist:.1f}mi from nearest center (limit: {self.radius_miles:.0f}mi)",
            )
        return FilterVerdict.ok(self.name)


@dataclass(frozen=True, slots=True)
class GarbageFilter:
    """Reject listings with garbage titles or locations (UI artifacts).

    Runs post-enrichment so we give detail page extraction a chance to
    recover real titles from data-sjs before rejecting.
    """

    name: str = "garbage"
    stage: FilterStage = FilterStage.POST_ENRICHMENT
    exempt_tags: frozenset[str] = frozenset()

    def __call__(self, listing: Listing) -> FilterVerdict:
        title_lower = (listing.title or "").strip().lower()
        title_clean = _LISTED_AGO_RE.sub("", title_lower).strip()
        location_lower = (listing.location or "").strip().lower()
        desc_len = len((listing.description or "").strip())

        # UI artifacts in title or location.
        if title_lower in _GARBAGE_TITLES:
            return FilterVerdict.reject(self.name, f"garbage title: '{title_lower}'")
        if location_lower and location_lower in _GARBAGE_LOCATIONS:
            return FilterVerdict.reject(self.name, f"garbage location: '{location_lower}'")

        # Sale events / curb alerts are not items (structural reject — not a
        # deal-quality judgment, so it stays watchlist-safe: a real watched
        # product is never a bare sale-event title).
        sale_event = _SALE_EVENT_RE.search(title_clean)
        if sale_event:
            return FilterVerdict.reject(
                self.name, f"non-item sale event: '{sale_event.group(0)}'"
            )

        # Unenrichable placeholder titles — only reject if description
        # is also too short for VLM to reason from.
        if title_clean in _UNENRICHABLE_TITLES and desc_len < 20:
            return FilterVerdict.reject(
                self.name,
                f"unenrichable title: '{listing.title}' (desc_len={desc_len})",
            )

        return FilterVerdict.ok(self.name)


# ---------------------------------------------------------------------------
# Filter chain
# ---------------------------------------------------------------------------

class FilterChain:
    """Runs filters at the appropriate stage, respects exemptions, logs results."""

    def __init__(self, filters: list[SponsoredFilter | KnownFarLocationFilter | LocationTextFilter | CategoryFilter | FreshnessFilter | GeoDistanceFilter | GarbageFilter]) -> None:
        self._filters = list(filters)
        self._by_stage: dict[FilterStage, list] = {}
        for f in self._filters:
            self._by_stage.setdefault(f.stage, []).append(f)

    def check(
        self,
        listing: Listing,
        stage: FilterStage,
        tags: frozenset[str] = frozenset(),
    ) -> FilterResult:
        """Run all filters for the given stage on a single listing.

        Args:
            listing: The listing to check.
            stage: Current pipeline stage.
            tags: Listing-level tags granting exemptions.

        Returns:
            FilterResult with all verdicts.
        """
        applicable = self._by_stage.get(stage, [])
        result = FilterResult(listing_id=listing.external_id or listing.id or "")

        for f in applicable:
            if tags & f.exempt_tags:
                verdict = FilterVerdict.ok(f.name)
            else:
                verdict = f(listing)

            result.verdicts.append(verdict)

            if not verdict.passed:
                result.passed = False

        return result

    def filter_batch(
        self,
        listings: list[Listing],
        stage: FilterStage,
        tags_by_id: dict[str, frozenset[str]] | None = None,
    ) -> tuple[list[Listing], list[tuple[Listing, FilterResult]]]:
        """Filter a batch of listings, returning (kept, rejected_with_reasons).

        Args:
            listings: Listings to filter.
            stage: Current pipeline stage.
            tags_by_id: Map of external_id → tags for exemptions.

        Returns:
            Tuple of (kept_listings, rejected_listings_with_results).
        """
        tags_map = tags_by_id or {}
        kept: list[Listing] = []
        rejected: list[tuple[Listing, FilterResult]] = []

        for listing in listings:
            eid = listing.external_id or listing.id or ""
            tags = tags_map.get(eid, frozenset())
            result = self.check(listing, stage, tags)

            if result.passed:
                kept.append(listing)
            else:
                rejected.append((listing, result))

        # Log summary if any rejected.
        if rejected:
            # Group rejections by filter name for concise logging.
            reasons: dict[str, int] = {}
            for _, res in rejected:
                for v in res.rejections:
                    reasons[v.filter_name] = reasons.get(v.filter_name, 0) + 1
            log.info(
                "filter_chain.batch_result",
                stage=stage.name,
                kept=len(kept),
                rejected=len(rejected),
                by_filter=reasons,
            )

        return kept, rejected
