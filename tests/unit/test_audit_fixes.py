"""Tests for audit fixes: price overestimation, seller-stated prices, garbage filtering."""

from __future__ import annotations

import pytest

from agentic_scraper.skills.ebay_lookup import compute_price_stats
from agentic_scraper.skills.orchestrator import (
    _build_validated_ebay_query,
    _extract_seller_stated_price,
    _get_unbranded_value_cap,
    _listing_has_no_brand,
    _sanity_check_msrp,
    _sanitize_untrusted_enrichment,
    _should_bypass_triage,
    _significant_words,
    _vlm_identified_known_brand,
    _word_overlap_ratio,
    extract_condition_signals,
)
from agentic_scraper.skills.models import PriceLookupResult, VisualEnrichment
from agentic_scraper.storage.models import Listing


# --- _build_validated_ebay_query ---


class TestBuildValidatedEbayQuery:
    """Cross-validation between Vision API product name and listing title."""

    def test_good_overlap_uses_enriched_name(self):
        """When Vision API and title agree, use the more specific enriched name."""
        result = _build_validated_ebay_query(
            enriched_name="Sony WH-1000XM4 Headphones",
            enriched_brand="Sony",
            listing_title="Sony WH-1000XM4",
            listing_description=None,
        )
        assert result.trusted is True
        assert "Sony" in result.query
        assert "WH-1000XM4" in result.query

    def test_divergent_uses_listing_title(self):
        """When Vision API diverges from title, trust the seller's title."""
        result = _build_validated_ebay_query(
            enriched_name="Herman Miller Aeron Chair",
            enriched_brand="Herman Miller",
            listing_title="Office chair",
            listing_description="No longer need a home office",
        )
        assert result.trusted is False
        assert "Office chair" in result.query
        assert "Herman Miller" not in result.query

    def test_garbage_title_uses_enriched(self):
        """When title is garbage (UI artifact), fall back to enriched name."""
        result = _build_validated_ebay_query(
            enriched_name="Canon Pro-1000 Printer",
            enriched_brand="Canon",
            listing_title="See details",
            listing_description=None,
        )
        assert result.trusted is True
        assert "Canon" in result.query
        assert "Pro-1000" in result.query

    def test_empty_title_uses_enriched(self):
        """When title is empty, fall back to enriched name."""
        result = _build_validated_ebay_query(
            enriched_name="KitchenAid Mixer",
            enriched_brand="KitchenAid",
            listing_title="",
            listing_description=None,
        )
        assert result.trusted is True
        assert "KitchenAid" in result.query

    def test_brand_in_description_uses_enriched(self):
        """When the brand appears in description, trust the enriched name."""
        result = _build_validated_ebay_query(
            enriched_name="Breville Barista Express",
            enriched_brand="Breville",
            listing_title="Espresso machine",
            listing_description="Breville espresso machine, works great",
        )
        assert result.trusted is True
        assert "Breville" in result.query

    def test_no_brand_generic_uses_title(self):
        """Generic enrichment with no brand match uses listing title."""
        result = _build_validated_ebay_query(
            enriched_name="Fine China Dinnerware Set",
            enriched_brand=None,
            listing_title="Baby Dish Set",
            listing_description="3 piece set",
        )
        assert result.trusted is False
        assert "Baby Dish Set" in result.query
        assert "Fine China" not in result.query


# --- _extract_seller_stated_price ---


class TestExtractSellerStatedPrice:
    """Extract prices sellers state they originally paid."""

    def test_paid_pattern(self):
        assert _extract_seller_stated_price("Paid $265. Only had it 2 months") == 265.0

    def test_originally_pattern(self):
        assert _extract_seller_stated_price("Originally $400, selling for $200") == 400.0

    def test_retail_pattern(self):
        assert _extract_seller_stated_price("Retails for $1,200 new") == 1200.0

    def test_msrp_pattern(self):
        assert _extract_seller_stated_price("MSRP $599") == 599.0

    def test_was_pattern(self):
        assert _extract_seller_stated_price("Was $150, asking $50") == 150.0

    def test_no_match(self):
        assert _extract_seller_stated_price("Great condition, pickup only") is None

    def test_none_input(self):
        assert _extract_seller_stated_price(None) is None

    def test_empty_string(self):
        assert _extract_seller_stated_price("") is None

    def test_bought_for_pattern(self):
        assert _extract_seller_stated_price("Bought it for $89.99 last year") == 89.99


# --- _listing_has_no_brand ---


class TestListingHasNoBrand:
    """Check if a listing has no identifiable brand."""

    def test_no_brand(self):
        listing = Listing(title="Office chair", description="Adjustable height")
        assert _listing_has_no_brand(listing) is True

    def test_brand_in_title(self):
        listing = Listing(title="Sony headphones", description="Wireless")
        assert _listing_has_no_brand(listing) is False

    def test_brand_in_description(self):
        listing = Listing(title="Headphones", description="These are Sony WH-1000XM4")
        assert _listing_has_no_brand(listing) is False

    def test_brand_in_metadata(self):
        listing = Listing(
            title="Stand mixer", description="", raw_data={"brand": "KitchenAid"}
        )
        assert _listing_has_no_brand(listing) is False

    def test_generic_brand_metadata(self):
        listing = Listing(
            title="Chair", description="", raw_data={"brand": "unbranded"}
        )
        assert _listing_has_no_brand(listing) is True

    def test_baby_dish_set(self):
        listing = Listing(title="New Baby Dish Set", description="3 piece set")
        assert _listing_has_no_brand(listing) is True

    def test_canon_printer(self):
        listing = Listing(
            title="Canon pro 1000",
            description="Free. Have 2 canon pro 1000 that don't work.",
        )
        assert _listing_has_no_brand(listing) is False

    def test_kitchenaid(self):
        listing = Listing(title="Kitchen Aid mixer", description="Works great")
        assert _listing_has_no_brand(listing) is False


# --- IQR outlier removal in compute_price_stats ---


class TestComputePriceStatsIQR:
    """IQR outlier removal prevents inflated medians."""

    def test_outlier_removal(self):
        """Mix of cheap and expensive items should remove outliers."""
        # Baby plates ($5-20) mixed with fine china ($400-663)
        prices = [5.0, 8.0, 12.0, 15.0, 18.0, 20.0, 400.0, 500.0, 663.0]
        result = compute_price_stats(prices, "baby dish set", "ebay")
        # After IQR removal, the $400-663 outliers should be dropped
        assert result.median_price < 50.0, f"Median {result.median_price} should be < $50"

    def test_consistent_prices_unchanged(self):
        """Prices within normal range should not be filtered."""
        prices = [80.0, 90.0, 100.0, 110.0, 120.0]
        result = compute_price_stats(prices, "headphones", "ebay")
        assert result.median_price == 100.0
        assert result.sample_count == 5

    def test_too_few_prices_skips_iqr(self):
        """With < 4 prices, IQR is not applied."""
        prices = [10.0, 500.0, 1000.0]
        result = compute_price_stats(prices, "widget", "ebay")
        assert result.sample_count == 3
        assert result.median_price == 500.0  # No filtering

    def test_empty_prices(self):
        result = compute_price_stats([], "nothing", "ebay")
        assert result.sample_count == 0
        assert result.confidence == 0.0


# --- _significant_words / _word_overlap_ratio ---


class TestWordOverlap:
    """Word overlap utilities for cross-validation."""

    def test_significant_words_strips_stopwords(self):
        words = _significant_words("A new office chair for sale")
        assert "office" in words
        # "chair" is a generic product category word → stripped
        assert "chair" not in words
        assert "a" not in words
        assert "for" not in words
        assert "new" not in words
        # Brand names should survive
        words2 = _significant_words("Sony WH-1000XM4 Headphones")
        assert "sony" in words2
        assert "1000xm4" in words2

    def test_overlap_identical(self):
        a = _significant_words("Sony WH-1000XM4 Headphones")
        b = _significant_words("Sony WH-1000XM4")
        assert _word_overlap_ratio(a, b) >= 0.5

    def test_overlap_divergent(self):
        a = _significant_words("Herman Miller Aeron Chair")
        b = _significant_words("Office chair")
        ratio = _word_overlap_ratio(a, b)
        assert ratio < 0.4, f"Overlap {ratio} should be low for divergent products"

    def test_overlap_empty(self):
        assert _word_overlap_ratio(set(), {"hello"}) == 0.0
        assert _word_overlap_ratio({"hello"}, set()) == 0.0


# --- _vlm_identified_known_brand ---


class TestVLMIdentifiedKnownBrand:
    """VLM brand detection bypass for unbranded cap."""

    def test_vlm_sees_sony(self):
        assert _vlm_identified_known_brand("Sony WH-1000XM4 Headphones") == "sony"

    def test_vlm_sees_kitchenaid(self):
        assert _vlm_identified_known_brand("KitchenAid Artisan Stand Mixer") == "kitchenaid"

    def test_vlm_generic_description(self):
        """Generic VLM output should NOT bypass cap."""
        assert _vlm_identified_known_brand("Office chair, adjustable height") is None

    def test_vlm_empty(self):
        assert _vlm_identified_known_brand("") is None

    def test_vlm_none(self):
        assert _vlm_identified_known_brand(None) is None

    def test_vlm_sees_ps5(self):
        assert _vlm_identified_known_brand("PS5 Digital Edition Console") == "ps5"

    def test_vlm_sees_dyson(self):
        """VLM identifies Dyson from photos even if listing says 'vacuum'."""
        assert _vlm_identified_known_brand("Dyson V15 Detect Cordless Vacuum") == "dyson"


# --- _should_bypass_triage ---


class TestShouldBypassTriage:
    """Triage bypass logic for garbage titles and watchlist items."""

    def test_watchlist_tagged_bypasses(self):
        """Listings from watchlist searches skip triage entirely."""
        listing = Listing(
            title="Sony WH-1000XM4",
            description="Like new",
            raw_data={"_watch_item_id": "watch_123"},
        )
        assert _should_bypass_triage(listing) == "watchlist_tagged"

    def test_normal_listing_no_bypass(self):
        """Regular listings with decent titles go through triage."""
        listing = Listing(
            title="Sony headphones wireless",
            description="Great condition, barely used",
        )
        assert _should_bypass_triage(listing) is None

    def test_garbage_title_bypasses(self):
        """Garbage titles like 'just listed' bypass triage for VLM."""
        listing = Listing(title="Just listed", description="")
        result = _should_bypass_triage(listing)
        assert result is not None
        assert "garbage" in result

    def test_insufficient_text_bypasses(self):
        """Very short text bypasses triage — VLM with image is better."""
        listing = Listing(title="Hi", description="")
        result = _should_bypass_triage(listing)
        assert result is not None
        assert "insufficient" in result

    def test_watchlist_without_id_no_bypass(self):
        """Listing with raw_data but no _watch_item_id doesn't bypass."""
        listing = Listing(
            title="Office chair",
            description="Adjustable",
            raw_data={"source": "category_sweep"},
        )
        assert _should_bypass_triage(listing) is None

    def test_empty_raw_data_no_bypass(self):
        """Listing with no raw_data doesn't crash."""
        listing = Listing(title="Nice couch", description="Pickup only")
        assert _should_bypass_triage(listing) is None


# --- _sanitize_untrusted_enrichment ---


class TestSanitizeUntrustedEnrichment:
    """Strip misleading Vision API output when cross-validation fails."""

    def test_strips_product_name(self):
        enrichment = VisualEnrichment(
            enriched_product_name="Analog Watch",
            enriched_brand="Casio",
            enriched_model="F-91W",
            enrichment_confidence=0.8,
        )
        result = _sanitize_untrusted_enrichment(enrichment)
        assert result.enriched_product_name is None
        assert result.enriched_brand is None
        assert result.enriched_model is None
        assert result.enrichment_confidence == 0.0

    def test_preserves_other_fields(self):
        """OCR text and web entities survive sanitization."""
        enrichment = VisualEnrichment(
            enriched_product_name="Standing Desk",
            enriched_brand="IKEA",
            enriched_model=None,
            enrichment_confidence=0.6,
            ocr_text="BEKANT",
            web_entities=[{"description": "IKEA desk"}, {"description": "office furniture"}],
        )
        result = _sanitize_untrusted_enrichment(enrichment)
        assert result.enriched_product_name is None
        assert result.enriched_brand is None
        assert result.enrichment_confidence == 0.0
        # These should survive
        assert result.ocr_text == "BEKANT"
        assert result.web_entities == [{"description": "IKEA desk"}, {"description": "office furniture"}]

    def test_already_none_fields(self):
        """Sanitizing enrichment that's already empty doesn't crash."""
        enrichment = VisualEnrichment(
            enriched_product_name=None,
            enriched_brand=None,
            enriched_model=None,
            enrichment_confidence=0.0,
        )
        result = _sanitize_untrusted_enrichment(enrichment)
        assert result.enriched_product_name is None
        assert result.enrichment_confidence == 0.0


# --- _sanity_check_msrp ---


class TestSanityCheckMSRP:
    """Cross-validate retail MSRP against listing price."""

    def _make_comparables(self, price: float) -> PriceLookupResult:
        return PriceLookupResult(
            median_price=price,
            average_price=price,
            min_price=price,
            max_price=price,
            sample_count=1,
            source="retail",
            search_query="test query",
            confidence=0.5,
        )

    def test_reasonable_msrp_accepted(self):
        """MSRP within 15x of listing price passes through."""
        listing = Listing(title="Coffee maker", price=20.0)
        comparables = self._make_comparables(169.99)  # 8.5x — reasonable
        result = _sanity_check_msrp(comparables, listing, "Keurig coffee maker")
        assert result.median_price == 169.99
        assert result.sample_count == 1

    def test_wildly_inflated_msrp_rejected(self):
        """MSRP at 80x listing price is rejected as hallucination."""
        listing = Listing(title="Floating tv shelf stand", price=10.0)
        comparables = self._make_comparables(799.0)  # 79.9x — absurd
        result = _sanity_check_msrp(comparables, listing, "Floating tv shelf stand")
        assert result.sample_count == 0
        assert result.confidence == 0.0

    def test_free_listing_skips_ratio_check(self):
        """Free/near-free listings can have any MSRP (legitimate giveaway)."""
        listing = Listing(title="Old printer", price=0.0)
        comparables = self._make_comparables(500.0)
        result = _sanity_check_msrp(comparables, listing, "Canon printer")
        assert result.median_price == 500.0
        assert result.sample_count == 1

    def test_cheap_listing_skips_ratio_check(self):
        """Listings under $5 skip ratio check (close to free)."""
        listing = Listing(title="Old books", price=3.0)
        comparables = self._make_comparables(200.0)  # 66x but under threshold
        result = _sanity_check_msrp(comparables, listing, "Books")
        assert result.median_price == 200.0

    def test_empty_comparables_pass_through(self):
        """Zero-sample comparables are returned as-is."""
        listing = Listing(title="Widget", price=10.0)
        comparables = PriceLookupResult(
            sample_count=0, source="retail", search_query="widget", confidence=0.0,
        )
        result = _sanity_check_msrp(comparables, listing, "Widget")
        assert result.sample_count == 0

    def test_borderline_ratio_accepted(self):
        """MSRP at exactly 15x listing price is accepted."""
        listing = Listing(title="Shelf", price=10.0)
        comparables = self._make_comparables(150.0)  # exactly 15x
        result = _sanity_check_msrp(comparables, listing, "Shelf")
        assert result.median_price == 150.0

    def test_just_over_ratio_rejected(self):
        """MSRP just over 15x listing price is rejected."""
        listing = Listing(title="Shelf", price=10.0)
        comparables = self._make_comparables(151.0)  # 15.1x
        result = _sanity_check_msrp(comparables, listing, "Shelf")
        assert result.sample_count == 0


# --- _get_unbranded_value_cap (power tools) ---


class TestUnbrandedValueCap:
    """High-value categories get a $500 cap instead of $100."""

    def test_default_cap(self):
        assert _get_unbranded_value_cap("Office chair") == 100.0

    def test_tv_high_cap(self):
        assert _get_unbranded_value_cap("55 inch TV") == 500.0

    def test_table_saw_high_cap(self):
        assert _get_unbranded_value_cap("Table saw with fence") == 500.0

    def test_band_saw_high_cap(self):
        assert _get_unbranded_value_cap("Band saw 14 inch") == 500.0

    def test_drill_press_high_cap(self):
        assert _get_unbranded_value_cap("Floor drill press") == 500.0

    def test_miter_saw_high_cap(self):
        assert _get_unbranded_value_cap("10 inch miter saw") == 500.0

    def test_welder_high_cap(self):
        assert _get_unbranded_value_cap("MIG welder 110v") == 500.0

    def test_pressure_washer_high_cap(self):
        assert _get_unbranded_value_cap("Pressure washer 3000 PSI") == 500.0

    def test_dovetail_jig_high_cap(self):
        assert _get_unbranded_value_cap("Dovetail jig and router") == 500.0


# --- _KNOWN_BRANDS (tool brands) ---


class TestKnownBrandsTools:
    """Tool brands should be recognized to bypass unbranded cap."""

    def test_rikon_recognized(self):
        assert _vlm_identified_known_brand("Rikon 10-326 Bandsaw") == "rikon"

    def test_porter_cable_recognized(self):
        assert _vlm_identified_known_brand("Porter Cable 4212 Dovetail Jig") == "porter cable"

    def test_festool_recognized(self):
        assert _vlm_identified_known_brand("Festool Domino DF 500") == "festool"

    def test_ridgid_recognized(self):
        assert _vlm_identified_known_brand("Ridgid R4512 Table Saw") == "ridgid"

    def test_grizzly_recognized(self):
        assert _vlm_identified_known_brand("Grizzly G0555 Bandsaw") == "grizzly"

    def test_dremel_recognized(self):
        assert _vlm_identified_known_brand("Dremel 4000 Rotary Tool") == "dremel"

    def test_listing_has_brand_porter_cable(self):
        """_listing_has_no_brand should detect Porter Cable."""
        listing = Listing(
            title="Porter Cable dovetail jig",
            description="Works great",
        )
        assert _listing_has_no_brand(listing) is False

    def test_listing_has_brand_rikon(self):
        listing = Listing(
            title="Rikon Mortis Machine",
            description="14 inch bandsaw",
        )
        assert _listing_has_no_brand(listing) is False


# --- Cross-validation synonyms ---


class TestCrossValidationSynonyms:
    """Synonym normalization in word overlap prevents false Vision API mismatches."""

    def test_tv_matches_television(self):
        """'tv' in title should match 'television' from Vision API."""
        title_words = _significant_words("Brand New 13.3 tv with dvd player")
        enriched_words = _significant_words("Television")
        ratio = _word_overlap_ratio(title_words, enriched_words)
        assert ratio > 0.0, f"TV and Television should match, got {ratio}"

    def test_fridge_matches_refrigerator(self):
        title_words = _significant_words("Mini fridge for sale")
        enriched_words = _significant_words("Refrigerator compact")
        ratio = _word_overlap_ratio(title_words, enriched_words)
        assert ratio > 0.0

    def test_espresso_matches_cappuccino(self):
        title_words = _significant_words("Espresso shot mug handmade")
        enriched_words = _significant_words("Cappuccino cup ceramic")
        ratio = _word_overlap_ratio(title_words, enriched_words)
        assert ratio > 0.0

    def test_unrelated_still_zero(self):
        """Unrelated items should still have zero overlap."""
        title_words = _significant_words("KitchenAid Utensils Set")
        enriched_words = _significant_words("Brush")
        ratio = _word_overlap_ratio(title_words, enriched_words)
        assert ratio == 0.0

    def test_exact_match_still_works(self):
        """Exact word matches should still work normally."""
        title_words = _significant_words("Sony WH-1000XM4 Headphones")
        enriched_words = _significant_words("Sony WH-1000XM4")
        ratio = _word_overlap_ratio(title_words, enriched_words)
        assert ratio >= 0.5


# --- Condition signal extraction ---


class TestConditionSignals:
    """Programmatic condition detection from listing text."""

    def test_sealed_nib(self):
        listing = Listing(title="Lego Star Wars Sealed sets NIB", description="")
        signals = extract_condition_signals(listing)
        assert "sealed/NIB" in signals["positive"]

    def test_like_new(self):
        listing = Listing(title="Coffee maker", description="Like new, barely used")
        signals = extract_condition_signals(listing)
        assert "like new" in signals["positive"]
        assert "barely used" in signals["positive"]

    def test_missing_parts(self):
        listing = Listing(title="Coffee maker", description="missing cord, works fine")
        signals = extract_condition_signals(listing)
        assert "missing parts" in signals["negative"]
        assert "works perfectly" in signals["positive"]

    def test_not_working(self):
        listing = Listing(title="Old printer", description="Doesn't work, for parts only")
        signals = extract_condition_signals(listing)
        assert "not working" in signals["negative"]
        assert "parts only" in signals["negative"]

    def test_no_signals(self):
        listing = Listing(title="Chair", description="Pickup in Madison")
        signals = extract_condition_signals(listing)
        assert signals["positive"] == []
        assert signals["negative"] == []

    def test_scratched(self):
        listing = Listing(title="Table", description="Some scratches on top, dented leg")
        signals = extract_condition_signals(listing)
        assert "scratched/worn" in signals["negative"]

    def test_with_box(self):
        listing = Listing(title="Headphones", description="Includes original box and manual")
        signals = extract_condition_signals(listing)
        assert "with box/manual" in signals["positive"]


# --- Model number extraction ---


class TestExtractModelNumber:
    """Extract product model numbers from text (OCR, title, description)."""

    def test_sony_headphones(self):
        """Standard alphanumeric model: WH-1000XM4."""
        from agentic_scraper.skills.orchestrator import extract_model_numbers
        results = extract_model_numbers("Sony WH-1000XM4 Wireless Headphones")
        assert "WH-1000XM4" in results

    def test_kitchenaid_mixer(self):
        """KitchenAid model number with letters and digits."""
        from agentic_scraper.skills.orchestrator import extract_model_numbers
        results = extract_model_numbers("KitchenAid KSM150PSER Artisan Stand Mixer")
        assert "KSM150PSER" in results

    def test_dewalt_saw(self):
        """DeWalt model with prefix letters + digits."""
        from agentic_scraper.skills.orchestrator import extract_model_numbers
        results = extract_model_numbers("DEWALT DWE7491RS 10-Inch Table Saw")
        assert "DWE7491RS" in results

    def test_samsung_tv(self):
        """Samsung TV model number pattern."""
        from agentic_scraper.skills.orchestrator import extract_model_numbers
        results = extract_model_numbers("Samsung QN65Q80AAFXZA 65\" QLED TV")
        assert "QN65Q80AAFXZA" in results

    def test_no_model_number(self):
        """Plain text with no model numbers returns empty."""
        from agentic_scraper.skills.orchestrator import extract_model_numbers
        results = extract_model_numbers("Nice wooden table for sale")
        assert results == []

    def test_multiple_models(self):
        """Multiple model numbers in one text."""
        from agentic_scraper.skills.orchestrator import extract_model_numbers
        results = extract_model_numbers("Bose QC35II with Sony WH-1000XM5 case")
        assert len(results) >= 2

    def test_ignores_short_codes(self):
        """Short generic codes like 'TV' or '4K' should not be model numbers."""
        from agentic_scraper.skills.orchestrator import extract_model_numbers
        results = extract_model_numbers("4K TV great condition")
        assert results == []

    def test_model_from_ocr_text(self):
        """Model number embedded in OCR text (typical Vision API output)."""
        from agentic_scraper.skills.orchestrator import extract_model_numbers
        ocr = "MODEL: DWE7491RS SERIAL: 20230415 MADE IN MEXICO"
        results = extract_model_numbers(ocr)
        assert "DWE7491RS" in results


# --- Title quality heuristics ---


class TestTitleQualityScore:
    """Score listing title quality — spam/scam signals reduce score."""

    def test_normal_title(self):
        """Well-formed title gets high score."""
        from agentic_scraper.skills.orchestrator import compute_title_quality_score
        score = compute_title_quality_score("Sony WH-1000XM4 Wireless Headphones")
        assert score >= 0.8

    def test_all_caps(self):
        """ALL CAPS title gets penalized."""
        from agentic_scraper.skills.orchestrator import compute_title_quality_score
        score = compute_title_quality_score("AMAZING DEAL MUST SEE BEST PRICE EVER")
        assert score < 0.6

    def test_emoji_spam(self):
        """Emoji-heavy title gets penalized."""
        from agentic_scraper.skills.orchestrator import compute_title_quality_score
        score = compute_title_quality_score("🔥🔥🔥 INCREDIBLE DEAL 💰💰💰 WOW 🎉🎉")
        assert score < 0.5

    def test_excessive_punctuation(self):
        """Excessive punctuation (!!! ???) gets penalized."""
        from agentic_scraper.skills.orchestrator import compute_title_quality_score
        score = compute_title_quality_score("Great deal!!! Must see!!! Won't last!!!")
        assert score < 0.7

    def test_short_title(self):
        """Very short title (low info) gets penalized."""
        from agentic_scraper.skills.orchestrator import compute_title_quality_score
        score = compute_title_quality_score("Chair")
        assert score <= 0.7

    def test_price_in_title(self):
        """Price mentioned in title is a mild positive signal (transparency)."""
        from agentic_scraper.skills.orchestrator import compute_title_quality_score
        score_with = compute_title_quality_score("KitchenAid Mixer $150 OBO")
        score_without = compute_title_quality_score("KitchenAid Mixer")
        # Price in title shouldn't hurt
        assert score_with >= score_without - 0.1

    def test_spam_words(self):
        """Spam keywords (LOOK, WOW, HURRY) penalize score."""
        from agentic_scraper.skills.orchestrator import compute_title_quality_score
        score = compute_title_quality_score("LOOK WOW HURRY best deal you'll ever find")
        assert score < 0.6


# --- Listing freshness scoring ---


class TestListingFreshnessBonus:
    """Newer listings get a freshness bonus for deal evaluation."""

    def test_just_posted(self):
        """Listing from minutes ago gets highest bonus."""
        from datetime import datetime, timezone
        from agentic_scraper.skills.orchestrator import compute_freshness_bonus
        listing = Listing(
            title="Test", posted_at=datetime.now(timezone.utc),
        )
        bonus = compute_freshness_bonus(listing)
        assert bonus >= 0.9

    def test_one_hour_old(self):
        """1-hour-old listing still gets good bonus."""
        from datetime import datetime, timedelta, timezone
        from agentic_scraper.skills.orchestrator import compute_freshness_bonus
        listing = Listing(
            title="Test",
            posted_at=datetime.now(timezone.utc) - timedelta(hours=1),
        )
        bonus = compute_freshness_bonus(listing)
        assert 0.5 <= bonus <= 1.0

    def test_twelve_hours_old(self):
        """12-hour-old listing gets reduced bonus."""
        from datetime import datetime, timedelta, timezone
        from agentic_scraper.skills.orchestrator import compute_freshness_bonus
        listing = Listing(
            title="Test",
            posted_at=datetime.now(timezone.utc) - timedelta(hours=12),
        )
        bonus = compute_freshness_bonus(listing)
        assert bonus < 0.5

    def test_no_posted_at(self):
        """Missing posted_at returns neutral (0.5)."""
        from agentic_scraper.skills.orchestrator import compute_freshness_bonus
        listing = Listing(title="Test", posted_at=None)
        bonus = compute_freshness_bonus(listing)
        assert bonus == 0.5

    def test_three_days_old(self):
        """3-day-old listing gets minimal bonus."""
        from datetime import datetime, timedelta, timezone
        from agentic_scraper.skills.orchestrator import compute_freshness_bonus
        listing = Listing(
            title="Test",
            posted_at=datetime.now(timezone.utc) - timedelta(days=3),
        )
        bonus = compute_freshness_bonus(listing)
        assert bonus <= 0.2


# --- Multi-item / lot detection with price normalization ---


class TestDetectMultiItem:
    """Detect multi-item listings and compute per-unit price."""

    def test_explicit_quantity(self):
        """'3 Lego sets for $45' → detected as multi-item."""
        from agentic_scraper.skills.orchestrator import detect_multi_item
        result = detect_multi_item("3 Lego sets", "$45", 45.0)
        assert result is not None
        assert result["quantity"] == 3
        assert result["per_unit_price"] == 15.0

    def test_set_of_quantity(self):
        """'Set of 6 glasses' detected."""
        from agentic_scraper.skills.orchestrator import detect_multi_item
        result = detect_multi_item("Set of 6 wine glasses", "", 30.0)
        assert result is not None
        assert result["quantity"] == 6
        assert result["per_unit_price"] == 5.0

    def test_lot_of_items(self):
        """'Lot of 20 Hot Wheels' detected."""
        from agentic_scraper.skills.orchestrator import detect_multi_item
        result = detect_multi_item("Lot of 20 Hot Wheels cars", "", 40.0)
        assert result is not None
        assert result["quantity"] == 20
        assert result["per_unit_price"] == 2.0

    def test_single_item(self):
        """Single item listing returns None."""
        from agentic_scraper.skills.orchestrator import detect_multi_item
        result = detect_multi_item("KitchenAid Stand Mixer", "", 150.0)
        assert result is None

    def test_pair(self):
        """'Pair of speakers' → quantity 2."""
        from agentic_scraper.skills.orchestrator import detect_multi_item
        result = detect_multi_item("Pair of Bose speakers", "", 100.0)
        assert result is not None
        assert result["quantity"] == 2
        assert result["per_unit_price"] == 50.0

    def test_bundle_keyword(self):
        """'Bundle' keyword triggers detection even without explicit count."""
        from agentic_scraper.skills.orchestrator import detect_multi_item
        result = detect_multi_item("PS5 game bundle - 5 games", "", 100.0)
        assert result is not None
        assert result["quantity"] == 5

    def test_zero_price(self):
        """Free listing with multi-item: per_unit_price = 0."""
        from agentic_scraper.skills.orchestrator import detect_multi_item
        result = detect_multi_item("Box of 10 books", "", 0.0)
        assert result is not None
        assert result["quantity"] == 10
        assert result["per_unit_price"] == 0.0
