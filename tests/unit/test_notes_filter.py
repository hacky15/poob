"""Tests for hard preference enforcement (notes exclusion filter)."""

import pytest

from agentic_scraper.skills.orchestrator import (
    _extract_excluded_terms,
    _listing_contradicts_notes,
)
from agentic_scraper.storage.models import Listing


class TestExtractExcludedTerms:
    """Test negation pattern extraction from user notes."""

    def test_not_metal(self):
        assert "metal" in _extract_excluded_terms("not metal")

    def test_no_ikea(self):
        assert "ikea" in _extract_excluded_terms("no IKEA")

    def test_avoid_particle_board(self):
        terms = _extract_excluded_terms("avoid particle board")
        assert "particle board" in terms

    def test_without_wheels(self):
        assert "wheels" in _extract_excluded_terms("without wheels")

    def test_dont_want_plastic(self):
        assert "plastic" in _extract_excluded_terms("don't want plastic")

    def test_never_cast_iron(self):
        terms = _extract_excluded_terms("never cast iron")
        assert "cast iron" in terms

    def test_multiple_exclusions(self):
        terms = _extract_excluded_terms("not metal, avoid plastic")
        assert "metal" in terms
        assert "plastic" in terms

    def test_empty_notes(self):
        assert _extract_excluded_terms("") == []

    def test_no_negation(self):
        assert _extract_excluded_terms("modern style, wood finish") == []

    def test_max_three_words(self):
        terms = _extract_excluded_terms("not solid oak wood something else")
        # Should capture up to 3 words
        assert len(terms) == 1
        assert len(terms[0].split()) <= 3


class TestListingContradictsNotes:
    """Test listing rejection based on notes exclusions."""

    def test_metal_in_title_rejected(self):
        listing = Listing(title="Metal File Cabinet", description="Two drawer")
        result = _listing_contradicts_notes(listing, "not metal")
        assert result == "metal"

    def test_metal_in_description_rejected(self):
        listing = Listing(title="File Cabinet", description="Made of solid metal")
        result = _listing_contradicts_notes(listing, "not metal")
        assert result == "metal"

    def test_wood_cabinet_passes(self):
        listing = Listing(title="Wood File Cabinet", description="Solid oak")
        result = _listing_contradicts_notes(listing, "not metal")
        assert result is None

    def test_word_boundary_metallic_not_rejected(self):
        listing = Listing(title="Metallic Paint File Cabinet", description="")
        # "metallic" should NOT match "metal" due to word boundaries
        result = _listing_contradicts_notes(listing, "not metal")
        # This is a borderline case - "metallic" contains "metal" but
        # our word boundary regex should handle it
        # Actually \bmetal\b won't match "metallic" — correct behavior
        assert result is None

    def test_case_insensitive(self):
        listing = Listing(title="METAL FILE CABINET", description="")
        result = _listing_contradicts_notes(listing, "not metal")
        assert result == "metal"

    def test_no_exclusions_passes(self):
        listing = Listing(title="Metal File Cabinet", description="")
        result = _listing_contradicts_notes(listing, "modern style preferred")
        assert result is None

    def test_empty_notes_passes(self):
        listing = Listing(title="Metal File Cabinet", description="")
        result = _listing_contradicts_notes(listing, "")
        assert result is None

    def test_multi_word_exclusion(self):
        listing = Listing(
            title="File Cabinet", description="Made of particle board"
        )
        result = _listing_contradicts_notes(listing, "avoid particle board")
        assert result == "particle board"

    def test_ikea_excluded(self):
        listing = Listing(title="IKEA ALEX File Cabinet", description="")
        result = _listing_contradicts_notes(listing, "no IKEA")
        assert result == "ikea"
