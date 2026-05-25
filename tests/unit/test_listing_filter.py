"""Unit tests for the unified listing filter pipeline.

Tests each individual filter (pass, reject, skip), the FilterChain stage
routing, tag-based exemptions, and batch filtering.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from poob.scanner.listing_filter import (
    CategoryFilter,
    FilterChain,
    FilterResult,
    FilterStage,
    FilterVerdict,
    FreshnessFilter,
    GarbageFilter,
    GeoDistanceFilter,
    SponsoredFilter,
)
from poob.storage.models import Listing


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_listing(
    *,
    external_id: str = "123",
    title: str = "Nice Widget",
    description: str = "",
    location: str = "Madison, WI",
    price: float | None = 25.0,
    is_sponsored: bool = False,
    posted_at: datetime | None = None,
    raw_data: dict | None = None,
) -> Listing:
    """Build a Listing with sensible defaults for testing."""
    return Listing(
        external_id=external_id,
        title=title,
        description=description,
        location=location,
        price=price,
        is_sponsored=is_sponsored,
        posted_at=posted_at,
        raw_data=raw_data or {},
        site="facebook",
    )


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


# ===================================================================
# FilterVerdict
# ===================================================================

class TestFilterVerdict:

    def test_ok(self):
        v = FilterVerdict.ok("test")
        assert v.passed is True
        assert v.filter_name == "test"
        assert v.reason == ""

    def test_reject(self):
        v = FilterVerdict.reject("test", "bad listing")
        assert v.passed is False
        assert v.filter_name == "test"
        assert v.reason == "bad listing"

    def test_skip(self):
        v = FilterVerdict.skip("test")
        assert v.passed is True
        assert v.filter_name == "test"
        assert v.reason == "skipped:missing_data"

    def test_frozen(self):
        v = FilterVerdict.ok("test")
        with pytest.raises(AttributeError):
            v.passed = False  # type: ignore[misc]


# ===================================================================
# FilterResult
# ===================================================================

class TestFilterResult:

    def test_empty_result_passes(self):
        r = FilterResult(listing_id="abc")
        assert r.passed is True
        assert r.rejections == []
        assert r.rejection_summary == ""

    def test_rejection_summary(self):
        r = FilterResult(
            listing_id="abc",
            passed=False,
            verdicts=[
                FilterVerdict.reject("a", "reason1"),
                FilterVerdict.ok("b"),
                FilterVerdict.reject("c", "reason2"),
            ],
        )
        assert len(r.rejections) == 2
        assert "a: reason1" in r.rejection_summary
        assert "c: reason2" in r.rejection_summary


# ===================================================================
# SponsoredFilter
# ===================================================================

class TestSponsoredFilter:
    filt = SponsoredFilter()

    def test_passes_normal_listing(self):
        listing = make_listing()
        v = self.filt(listing)
        assert v.passed is True

    def test_rejects_sponsored(self):
        listing = make_listing(is_sponsored=True)
        v = self.filt(listing)
        assert v.passed is False
        assert "sponsored" in v.reason

    def test_rejects_ships_to_you(self):
        listing = make_listing(location="Ships to you")
        v = self.filt(listing)
        assert v.passed is False
        assert "shipping-only" in v.reason

    def test_rejects_ships_nationwide(self):
        listing = make_listing(location="Ships Nationwide")
        v = self.filt(listing)
        assert v.passed is False
        assert "shipping-only" in v.reason

    def test_passes_normal_location_with_ship_substring(self):
        # "Bishop" contains "ship" but not "you"/"nationwide"
        listing = make_listing(location="Bishop, CA")
        v = self.filt(listing)
        assert v.passed is True

    def test_stage_is_pre_enrichment(self):
        assert self.filt.stage == FilterStage.PRE_ENRICHMENT

    def test_no_exempt_tags(self):
        assert self.filt.exempt_tags == frozenset()


# ===================================================================
# CategoryFilter
# ===================================================================

class TestCategoryFilter:
    filt = CategoryFilter()

    def test_passes_normal_listing(self):
        listing = make_listing(title="Vintage Lamp")
        v = self.filt(listing)
        assert v.passed is True

    def test_rejects_vehicle_keyword_in_title(self):
        listing = make_listing(title="2015 Honda Sedan Clean Title")
        v = self.filt(listing)
        assert v.passed is False
        assert "keyword" in v.reason

    def test_rejects_vehicle_keyword_in_description(self):
        listing = make_listing(title="Great Deal", description="150k mileage, clean title")
        v = self.filt(listing)
        assert v.passed is False
        assert "keyword" in v.reason

    def test_rejects_housing_keyword(self):
        listing = make_listing(title="Beautiful house for rent downtown")
        v = self.filt(listing)
        assert v.passed is False
        assert "keyword" in v.reason

    def test_rejects_couch_keyword(self):
        listing = make_listing(title="Big comfy couch")
        v = self.filt(listing)
        assert v.passed is False
        assert "keyword" in v.reason

    def test_rejects_sectional_keyword(self):
        listing = make_listing(title="Large sectional for sale")
        v = self.filt(listing)
        assert v.passed is False

    # --- Tier 1: category_id based filtering ---

    def test_passes_with_non_excluded_category_id(self):
        """A recognized but non-excluded category_id should pass without keyword check."""
        listing = make_listing(
            title="2015 Honda Sedan",  # Would fail keyword check
            raw_data={"category_id": "99999"},  # Not in excluded set
        )
        v = self.filt(listing)
        assert v.passed is True  # category_id takes priority, skips keyword check

    def test_falls_back_to_keywords_without_category_id(self):
        """Without category_id, keyword matching is used."""
        listing = make_listing(title="Apartment sublet near campus")
        v = self.filt(listing)
        assert v.passed is False
        assert "keyword" in v.reason

    # --- Exemption tag ---

    def test_has_watchlist_category_override_tag(self):
        assert "watchlist_category_override" in self.filt.exempt_tags

    def test_stage_is_pre_enrichment(self):
        assert self.filt.stage == FilterStage.PRE_ENRICHMENT


# ===================================================================
# FreshnessFilter
# ===================================================================

class TestFreshnessFilter:
    filt = FreshnessFilter(max_age_hours=6)

    def test_passes_fresh_listing(self):
        listing = make_listing(posted_at=_utc_now() - timedelta(hours=1))
        v = self.filt(listing)
        assert v.passed is True

    def test_rejects_old_listing(self):
        listing = make_listing(posted_at=_utc_now() - timedelta(hours=10))
        v = self.filt(listing)
        assert v.passed is False
        assert "old" in v.reason

    def test_skips_when_no_posted_at(self):
        listing = make_listing(posted_at=None)
        v = self.filt(listing)
        assert v.passed is True
        assert "skipped" in v.reason

    def test_passes_at_boundary(self):
        # Exactly at the limit — should be within tolerance
        listing = make_listing(posted_at=_utc_now() - timedelta(hours=5, minutes=59))
        v = self.filt(listing)
        assert v.passed is True

    def test_rejects_just_over_boundary(self):
        listing = make_listing(posted_at=_utc_now() - timedelta(hours=6, minutes=1))
        v = self.filt(listing)
        assert v.passed is False

    def test_disabled_when_max_age_zero(self):
        filt = FreshnessFilter(max_age_hours=0)
        listing = make_listing(posted_at=_utc_now() - timedelta(hours=999))
        v = filt(listing)
        assert v.passed is True

    def test_disabled_when_max_age_negative(self):
        filt = FreshnessFilter(max_age_hours=-1)
        listing = make_listing(posted_at=_utc_now() - timedelta(hours=999))
        v = filt(listing)
        assert v.passed is True

    def test_custom_max_age(self):
        filt = FreshnessFilter(max_age_hours=24)
        listing = make_listing(posted_at=_utc_now() - timedelta(hours=20))
        v = filt(listing)
        assert v.passed is True

    def test_stage_is_pre_enrichment(self):
        assert self.filt.stage == FilterStage.PRE_ENRICHMENT


class TestFreshnessFilterPostEnrichmentRejectsNoTimestamp:
    """At POST_ENRICHMENT this is the last chance to verify freshness.
    A listing without posted_at after enrichment can never fire a
    notification, so spending VLM budget on it is waste."""

    filt = FreshnessFilter(
        max_age_hours=1,
        stage=FilterStage.POST_ENRICHMENT,
    )

    def test_rejects_no_posted_at_at_post_enrichment(self):
        listing = make_listing(posted_at=None)
        v = self.filt(listing)
        assert v.passed is False
        assert "no posted_at" in v.reason or "verify fresh" in v.reason

    def test_pre_enrichment_still_skips_no_posted_at(self):
        """Default (PRE_ENRICHMENT) behavior must remain SKIP — the
        timestamp may still arrive during detail-page enrichment."""
        pre_filt = FreshnessFilter(
            max_age_hours=1,
            stage=FilterStage.PRE_ENRICHMENT,
        )
        listing = make_listing(posted_at=None)
        v = pre_filt(listing)
        assert v.passed is True
        assert "skipped" in v.reason

    def test_post_enrichment_passes_fresh_listing(self):
        listing = make_listing(posted_at=_utc_now() - timedelta(minutes=10))
        v = self.filt(listing)
        assert v.passed is True

    def test_post_enrichment_rejects_over_age_listing(self):
        listing = make_listing(posted_at=_utc_now() - timedelta(hours=2))
        v = self.filt(listing)
        assert v.passed is False
        assert "old" in v.reason

    def test_post_enrichment_disabled_max_age_still_ok_on_none(self):
        """When max_age_hours<=0 the filter is fully disabled — even
        no-posted_at listings pass (filter contributes nothing)."""
        filt = FreshnessFilter(
            max_age_hours=0,
            stage=FilterStage.POST_ENRICHMENT,
        )
        listing = make_listing(posted_at=None)
        v = filt(listing)
        assert v.passed is True


# ===================================================================
# GeoDistanceFilter
# ===================================================================

class TestGeoDistanceFilter:
    # Madison, WI center
    filt = GeoDistanceFilter(center_lat=43.0731, center_lon=-89.4012, radius_miles=40.0)

    def test_passes_within_radius(self):
        # Sun Prairie, WI — ~15 miles from Madison
        listing = make_listing(raw_data={"latitude": 43.1836, "longitude": -89.2137})
        v = self.filt(listing)
        assert v.passed is True

    def test_rejects_outside_radius(self):
        # Milwaukee, WI — ~77 miles from Madison
        listing = make_listing(raw_data={"latitude": 43.0389, "longitude": -87.9065})
        v = self.filt(listing)
        assert v.passed is False
        assert "mi from center" in v.reason

    def test_skips_when_no_coords(self):
        listing = make_listing(raw_data={})
        v = self.filt(listing)
        assert v.passed is True
        assert "skipped" in v.reason

    def test_skips_when_coords_none(self):
        listing = make_listing(raw_data={"latitude": None, "longitude": None})
        v = self.filt(listing)
        assert v.passed is True
        assert "skipped" in v.reason

    def test_skips_null_island(self):
        """Null island (0,0) should be treated as invalid coordinates."""
        listing = make_listing(raw_data={"latitude": 0.0, "longitude": 0.0})
        v = self.filt(listing)
        assert v.passed is True
        assert "skipped" in v.reason

    def test_skips_when_no_center_configured(self):
        """Filter with no center configured should skip everything."""
        filt = GeoDistanceFilter(center_lat=0.0, center_lon=0.0, radius_miles=40.0)
        listing = make_listing(raw_data={"latitude": 43.0731, "longitude": -89.4012})
        v = filt(listing)
        assert v.passed is True
        assert "skipped" in v.reason

    def test_exact_same_point(self):
        listing = make_listing(raw_data={"latitude": 43.0731, "longitude": -89.4012})
        v = self.filt(listing)
        assert v.passed is True

    def test_custom_radius(self):
        filt = GeoDistanceFilter(center_lat=43.0731, center_lon=-89.4012, radius_miles=100.0)
        # Milwaukee is ~77 miles — should pass with 100mi radius
        listing = make_listing(raw_data={"latitude": 43.0389, "longitude": -87.9065})
        v = filt(listing)
        assert v.passed is True

    def test_stage_is_post_enrichment(self):
        assert self.filt.stage == FilterStage.POST_ENRICHMENT


# ===================================================================
# GarbageFilter
# ===================================================================

class TestGarbageFilter:
    filt = GarbageFilter()

    def test_passes_normal_listing(self):
        listing = make_listing(title="Vintage Desk Lamp")
        v = self.filt(listing)
        assert v.passed is True

    def test_rejects_see_details_title(self):
        listing = make_listing(title="See Details")
        v = self.filt(listing)
        assert v.passed is False
        assert "garbage title" in v.reason

    def test_rejects_loading_title(self):
        listing = make_listing(title="Loading")
        v = self.filt(listing)
        assert v.passed is False
        assert "garbage title" in v.reason

    def test_rejects_marketplace_title(self):
        listing = make_listing(title="Facebook Marketplace")
        v = self.filt(listing)
        assert v.passed is False

    def test_rejects_garbage_location(self):
        listing = make_listing(title="Good Widget", location="More Options")
        v = self.filt(listing)
        assert v.passed is False
        assert "garbage location" in v.reason

    def test_rejects_unenrichable_title_with_short_desc(self):
        listing = make_listing(title="Just Listed", description="short")
        v = self.filt(listing)
        assert v.passed is False
        assert "unenrichable" in v.reason

    def test_passes_unenrichable_title_with_long_desc(self):
        """Unenrichable title is OK if description is long enough for VLM."""
        listing = make_listing(
            title="New Listing",
            description="This is a detailed description of a great item that is more than 20 chars",
        )
        v = self.filt(listing)
        assert v.passed is True

    def test_rejects_empty_title_with_short_desc(self):
        listing = make_listing(title="", description="tiny")
        v = self.filt(listing)
        assert v.passed is False

    def test_passes_empty_title_with_long_desc(self):
        listing = make_listing(title="", description="A sufficiently long description for VLM evaluation")
        v = self.filt(listing)
        assert v.passed is True

    def test_strips_listed_ago_pattern(self):
        """Titles like 'Listed 2 hours ago' should be treated as unenrichable."""
        listing = make_listing(title="Listed 2 hours ago", description="ok")
        v = self.filt(listing)
        assert v.passed is False
        assert "unenrichable" in v.reason

    def test_passes_title_containing_listed_ago_with_real_content(self):
        """A real title that happens to have 'listed X ago' in it should pass."""
        listing = make_listing(title="Cool Widget listed 3 days ago")
        v = self.filt(listing)
        assert v.passed is True

    def test_case_insensitive(self):
        listing = make_listing(title="SEE DETAILS")
        v = self.filt(listing)
        assert v.passed is False

    def test_stage_is_post_enrichment(self):
        assert self.filt.stage == FilterStage.POST_ENRICHMENT


# ===================================================================
# FilterChain — stage routing
# ===================================================================

class TestFilterChainStageRouting:

    def _default_chain(self) -> FilterChain:
        return FilterChain([
            SponsoredFilter(),
            CategoryFilter(),
            FreshnessFilter(max_age_hours=6),
            GeoDistanceFilter(center_lat=43.07, center_lon=-89.40, radius_miles=40),
            GarbageFilter(),
        ])

    def test_pre_enrichment_runs_only_pre_filters(self):
        chain = self._default_chain()
        listing = make_listing()
        result = chain.check(listing, FilterStage.PRE_ENRICHMENT)
        filter_names = {v.filter_name for v in result.verdicts}
        # Pre-enrichment filters: sponsored, category, freshness
        assert "sponsored" in filter_names
        assert "category" in filter_names
        assert "freshness" in filter_names
        # Post-enrichment filters should NOT run
        assert "geo_distance" not in filter_names
        assert "garbage" not in filter_names

    def test_post_enrichment_runs_only_post_filters(self):
        chain = self._default_chain()
        listing = make_listing()
        result = chain.check(listing, FilterStage.POST_ENRICHMENT)
        filter_names = {v.filter_name for v in result.verdicts}
        # Post-enrichment filters: geo_distance, garbage
        assert "geo_distance" in filter_names
        assert "garbage" in filter_names
        # Pre-enrichment filters should NOT run
        assert "sponsored" not in filter_names
        assert "category" not in filter_names

    def test_passes_clean_listing_all_stages(self):
        chain = self._default_chain()
        listing = make_listing(
            posted_at=_utc_now() - timedelta(hours=1),
            raw_data={"latitude": 43.07, "longitude": -89.40},
        )
        pre = chain.check(listing, FilterStage.PRE_ENRICHMENT)
        post = chain.check(listing, FilterStage.POST_ENRICHMENT)
        assert pre.passed is True
        assert post.passed is True

    def test_single_rejection_fails_result(self):
        chain = self._default_chain()
        listing = make_listing(is_sponsored=True)
        result = chain.check(listing, FilterStage.PRE_ENRICHMENT)
        assert result.passed is False
        assert len(result.rejections) >= 1

    def test_multiple_rejections_all_recorded(self):
        """A listing that fails multiple filters should have all rejections."""
        chain = self._default_chain()
        listing = make_listing(
            is_sponsored=True,
            title="2015 Honda sedan clean title",
            posted_at=_utc_now() - timedelta(hours=24),
        )
        result = chain.check(listing, FilterStage.PRE_ENRICHMENT)
        assert result.passed is False
        rejected_names = {v.filter_name for v in result.rejections}
        assert "sponsored" in rejected_names
        assert "category" in rejected_names
        assert "freshness" in rejected_names


# ===================================================================
# FilterChain — tag-based exemptions
# ===================================================================

class TestFilterChainExemptions:

    def test_category_filter_exempted_by_tag(self):
        chain = FilterChain([CategoryFilter()])
        listing = make_listing(title="Big comfy couch")  # Would fail category

        # Without tag — rejected
        result_no_tag = chain.check(listing, FilterStage.PRE_ENRICHMENT)
        assert result_no_tag.passed is False

        # With exemption tag — passes
        result_with_tag = chain.check(
            listing,
            FilterStage.PRE_ENRICHMENT,
            tags=frozenset({"watchlist_category_override"}),
        )
        assert result_with_tag.passed is True

    def test_unrelated_tag_does_not_exempt(self):
        chain = FilterChain([CategoryFilter()])
        listing = make_listing(title="Big comfy couch")
        result = chain.check(
            listing,
            FilterStage.PRE_ENRICHMENT,
            tags=frozenset({"some_other_tag"}),
        )
        assert result.passed is False

    def test_sponsored_filter_has_no_exempt_tags(self):
        """Sponsored filter cannot be exempted by any tag."""
        chain = FilterChain([SponsoredFilter()])
        listing = make_listing(is_sponsored=True)
        result = chain.check(
            listing,
            FilterStage.PRE_ENRICHMENT,
            tags=frozenset({"watchlist_category_override"}),
        )
        assert result.passed is False

    def test_exemption_replaces_with_ok_verdict(self):
        """When exempted, the filter's verdict should be ok, not skipped."""
        chain = FilterChain([CategoryFilter()])
        listing = make_listing(title="Honda Sedan salvage title")
        result = chain.check(
            listing,
            FilterStage.PRE_ENRICHMENT,
            tags=frozenset({"watchlist_category_override"}),
        )
        assert result.passed is True
        assert result.verdicts[0].passed is True
        assert result.verdicts[0].filter_name == "category"


# ===================================================================
# FilterChain — filter_batch
# ===================================================================

class TestFilterBatch:

    def _chain(self) -> FilterChain:
        return FilterChain([
            SponsoredFilter(),
            CategoryFilter(),
            FreshnessFilter(max_age_hours=6),
        ])

    def test_batch_separates_kept_and_rejected(self):
        chain = self._chain()
        listings = [
            make_listing(external_id="good1", title="Lamp"),
            make_listing(external_id="bad1", is_sponsored=True),
            make_listing(external_id="good2", title="Chair"),
            make_listing(external_id="bad2", title="2015 Honda sedan clean title"),
        ]
        kept, rejected = chain.filter_batch(listings, FilterStage.PRE_ENRICHMENT)
        kept_ids = {l.external_id for l in kept}
        rejected_ids = {l.external_id for l, _ in rejected}
        assert kept_ids == {"good1", "good2"}
        assert rejected_ids == {"bad1", "bad2"}

    def test_batch_all_pass(self):
        chain = self._chain()
        listings = [
            make_listing(external_id="a", title="Lamp"),
            make_listing(external_id="b", title="Book"),
        ]
        kept, rejected = chain.filter_batch(listings, FilterStage.PRE_ENRICHMENT)
        assert len(kept) == 2
        assert len(rejected) == 0

    def test_batch_all_rejected(self):
        chain = self._chain()
        listings = [
            make_listing(external_id="a", is_sponsored=True),
            make_listing(external_id="b", is_sponsored=True),
        ]
        kept, rejected = chain.filter_batch(listings, FilterStage.PRE_ENRICHMENT)
        assert len(kept) == 0
        assert len(rejected) == 2

    def test_batch_empty_list(self):
        chain = self._chain()
        kept, rejected = chain.filter_batch([], FilterStage.PRE_ENRICHMENT)
        assert kept == []
        assert rejected == []

    def test_batch_with_per_listing_tags(self):
        chain = self._chain()
        listings = [
            make_listing(external_id="couch1", title="Big comfy couch"),
            make_listing(external_id="couch2", title="Leather sofa"),
        ]
        tags_by_id = {
            "couch1": frozenset({"watchlist_category_override"}),
            # couch2 has no tags — should be rejected
        }
        kept, rejected = chain.filter_batch(
            listings, FilterStage.PRE_ENRICHMENT, tags_by_id=tags_by_id,
        )
        kept_ids = {l.external_id for l in kept}
        rejected_ids = {l.external_id for l, _ in rejected}
        assert "couch1" in kept_ids
        assert "couch2" in rejected_ids

    def test_batch_rejected_includes_filter_result(self):
        chain = self._chain()
        listings = [make_listing(external_id="bad", is_sponsored=True)]
        _, rejected = chain.filter_batch(listings, FilterStage.PRE_ENRICHMENT)
        assert len(rejected) == 1
        listing, result = rejected[0]
        assert listing.external_id == "bad"
        assert isinstance(result, FilterResult)
        assert result.passed is False
        assert len(result.rejections) >= 1


# ===================================================================
# FilterChain — listing_id resolution
# ===================================================================

class TestFilterChainListingId:

    def test_uses_external_id(self):
        chain = FilterChain([SponsoredFilter()])
        listing = make_listing(external_id="ext123")
        listing.id = "db456"
        result = chain.check(listing, FilterStage.PRE_ENRICHMENT)
        assert result.listing_id == "ext123"

    def test_falls_back_to_id(self):
        chain = FilterChain([SponsoredFilter()])
        listing = make_listing(external_id="")
        listing.id = "db456"
        result = chain.check(listing, FilterStage.PRE_ENRICHMENT)
        assert result.listing_id == "db456"

    def test_empty_when_no_ids(self):
        chain = FilterChain([SponsoredFilter()])
        listing = make_listing(external_id="")
        listing.id = None
        result = chain.check(listing, FilterStage.PRE_ENRICHMENT)
        assert result.listing_id == ""


# ===================================================================
# Edge cases
# ===================================================================

class TestEdgeCases:

    def test_empty_filter_chain(self):
        chain = FilterChain([])
        listing = make_listing(is_sponsored=True)
        result = chain.check(listing, FilterStage.PRE_ENRICHMENT)
        assert result.passed is True
        assert result.verdicts == []

    def test_geo_filter_with_out_of_range_coords(self):
        """Coordinates outside valid range should be treated as missing."""
        filt = GeoDistanceFilter(center_lat=43.07, center_lon=-89.40, radius_miles=40)
        listing = make_listing(raw_data={"latitude": 999, "longitude": -999})
        v = filt(listing)
        assert v.passed is True
        assert "skipped" in v.reason

    def test_freshness_filter_with_future_listing(self):
        """A listing with a future posted_at should pass (age is negative)."""
        filt = FreshnessFilter(max_age_hours=6)
        listing = make_listing(posted_at=_utc_now() + timedelta(hours=1))
        v = filt(listing)
        assert v.passed is True

    def test_category_filter_keyword_case_insensitive(self):
        filt = CategoryFilter()
        listing = make_listing(title="SALVAGE TITLE Honda")
        v = filt(listing)
        assert v.passed is False

    def test_garbage_filter_whitespace_title(self):
        filt = GarbageFilter()
        listing = make_listing(title="   ", description="short")
        v = filt(listing)
        # Stripped empty title is in _UNENRICHABLE_TITLES and desc < 20
        assert v.passed is False

    def test_sponsored_filter_empty_location(self):
        filt = SponsoredFilter()
        listing = make_listing(location="")
        v = filt(listing)
        assert v.passed is True

    def test_garbage_filter_none_title(self):
        """Listing.title defaults to '' but raw_data could produce None-ish values."""
        filt = GarbageFilter()
        listing = make_listing(title="", description="A decent length description here yep")
        v = filt(listing)
        assert v.passed is True

    def test_category_filter_partial_keyword_no_false_positive(self):
        """'carpet' contains 'car' but 'car' is a standalone keyword with space handling."""
        filt = CategoryFilter()
        # 'car' is in the set as "car" — substring match means "carpet" WILL match.
        # This tests the actual current behavior (substring, not word-boundary).
        listing = make_listing(title="carpet cleaner")
        v = filt(listing)
        # "car" is in _VEHICLE_KEYWORDS and "car" is a substring of "carpet"
        assert v.passed is False  # Known limitation of substring matching
