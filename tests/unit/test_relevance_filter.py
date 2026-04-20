"""Tests for the post-fetch relevance filter.

Uses real garbage data from Facebook Marketplace searches to verify
the filter correctly drops irrelevant results while keeping genuine matches.

The filter is a LIGHT sanity check — it drops OBVIOUSLY wrong results
but lets domain-adjacent items through to the triage LLM.
"""

from __future__ import annotations

import pytest

from poob.sites.facebook.patrol_scanner import (
    _expand_keywords,
    _tokenize_query,
    filter_by_relevance,
)
from poob.storage.models import Listing


def _listing(title: str, price: float = 10.0) -> Listing:
    """Helper: create a minimal Listing with a title."""
    return Listing(
        site="facebook_marketplace",
        external_id="1234",
        title=title,
        price=price,
        location="Madison, WI",
        listing_url="https://facebook.com/marketplace/item/1234",
    )


# ---------------------------------------------------------------------------
# _tokenize_query
# ---------------------------------------------------------------------------

class TestTokenizeQuery:
    def test_basic(self):
        assert _tokenize_query("smart tv") == ["smart", "tv"]

    def test_strips_stopwords(self):
        assert _tokenize_query("over the toilet shelves") == ["toilet", "shelves"]

    def test_compound_brand(self):
        assert _tokenize_query("kitchenaid mixer") == ["kitchenaid", "mixer"]

    def test_multi_word_with_stopwords(self):
        assert _tokenize_query("stainless steel cookware") == ["stainless", "steel", "cookware"]

    def test_empty(self):
        assert _tokenize_query("") == []

    def test_all_stopwords(self):
        assert _tokenize_query("a the and") == []


# ---------------------------------------------------------------------------
# _expand_keywords
# ---------------------------------------------------------------------------

class TestExpandKeywords:
    def test_espresso_expands_to_coffee_domain(self):
        expanded = _expand_keywords(["espresso", "machine"])
        assert "coffee" in expanded
        assert "latte" in expanded
        assert "cappuccino" in expanded
        assert "nespresso" in expanded
        assert "maker" in expanded  # "machine" expands to "maker"
        assert "brewer" in expanded

    def test_tv_expands(self):
        expanded = _expand_keywords(["smart", "tv"])
        assert "television" in expanded
        assert "hdtv" in expanded
        assert "roku" in expanded
        assert "oled" in expanded

    def test_no_expansion_for_unknown(self):
        expanded = _expand_keywords(["ps5"])
        assert expanded == {"ps5"}

    def test_cookware_expands(self):
        expanded = _expand_keywords(["stainless", "steel", "cookware"])
        assert "pots" in expanded
        assert "pans" in expanded
        assert "skillet" in expanded


# ---------------------------------------------------------------------------
# filter_by_relevance — Smart TV
# ---------------------------------------------------------------------------

class TestSmartTvRelevance:
    """Real data: "smart tv" search returned fireplaces, stereo speakers, etc."""

    def test_keeps_actual_tvs(self):
        listings = [
            _listing("42\" TCL Roku Tv"),
            _listing("40 inch TV"),
            _listing("Samsung TV 29\""),
            _listing("32\" inch Onn Roku tv with Onn Soundbar"),
            _listing("MX3 AIR FLY MOUSE (Androids TV Boxes Smart TV, PCs, Projectors"),
        ]
        result = filter_by_relevance(listings, "smart tv")
        assert len(result) == 5

    def test_keeps_roku_via_expansion(self):
        """Roku is a related term for 'smart' — should pass."""
        listings = [_listing("50\" Roku Streaming Stick")]
        result = filter_by_relevance(listings, "smart tv")
        assert len(result) == 1

    def test_keeps_hdtv_via_expansion(self):
        """HDTV contains 'tv' substring — should pass."""
        listings = [_listing("Vizio Razor HDTV 22\"")]
        result = filter_by_relevance(listings, "smart tv")
        assert len(result) == 1

    def test_drops_garbage(self):
        listings = [
            _listing("Stereo Speakers"),
            _listing("Philips DVD Player & Recorder"),
            _listing("OEM Chevy Head Unit Radio Stero"),
            _listing("Blue ray movie group"),
        ]
        result = filter_by_relevance(listings, "smart tv")
        assert len(result) == 0


# ---------------------------------------------------------------------------
# filter_by_relevance — KitchenAid
# ---------------------------------------------------------------------------

class TestKitchenAidRelevance:
    """Real data: "kitchen aid" search returned 0 KitchenAid items."""

    def test_drops_all_garbage(self):
        listings = [
            _listing("Vintage Sunlite Dairy Eau Claire WIS Restaurant Creamer"),
            _listing("Electric fry pans"),
            _listing("Oven pads"),
            _listing("Pampered Chef Quick Slice"),
            _listing("Corelle Coordinates Casa Flora Set of 3 nesting mixing bowls"),
        ]
        result = filter_by_relevance(listings, "kitchenaid")
        assert len(result) == 0

    def test_keeps_stand_mixer_via_expansion(self):
        """'kitchenaid' related terms include 'stand mixer'."""
        listings = [
            _listing("KitchenAid Stand Mixer 5qt"),
            _listing("Kitchen Aid Artisan Mixer"),
            _listing("Random oven pads"),
        ]
        result = filter_by_relevance(listings, "kitchen aid")
        titles = [l.title for l in result]
        assert "KitchenAid Stand Mixer 5qt" in titles
        assert "Kitchen Aid Artisan Mixer" in titles
        assert "Random oven pads" not in titles


# ---------------------------------------------------------------------------
# filter_by_relevance — Espresso Machine
# ---------------------------------------------------------------------------

class TestEspressoMachineRelevance:
    """Real data: "espresso machine" returned coffee tables, watches, pants.

    With related-term expansion, coffee/latte/cappuccino items now pass
    through to the triage LLM instead of being killed at the filter.
    """

    def test_keeps_real_espresso(self):
        listings = [
            _listing("Coffee/Espresso Machine"),
            _listing("GeekChef Espresso Coffee Maker"),
            _listing("Expresso/Latte/Cappuccino maker"),
            _listing("Nespresso Machine"),
        ]
        result = filter_by_relevance(listings, "espresso machine")
        assert len(result) == 4

    def test_keeps_coffee_adjacent_via_expansion(self):
        """Coffee makers should pass through to triage, not be killed."""
        listings = [
            _listing("Keurig Coffee Maker Model Select K 80"),
            _listing("Ninja Drip Coffee Maker"),
            _listing("4 cup coffee pot"),
            _listing("Cold Coffee French Press"),
            _listing("Capresso 5 cup coffee maker"),
            _listing("Moka Pot"),  # moka is related to espresso
            _listing("Cappuccino machine toys"),
        ]
        result = filter_by_relevance(listings, "espresso machine")
        # All should pass — "coffee" is a related term for "espresso",
        # "maker/press" are related terms for "machine", "moka" is related
        assert len(result) == 7

    def test_drops_garbage(self):
        listings = [
            _listing("2 Pair Gloria Vanderbilt Capri Pants Size 18W"),
            _listing("Russian Stop Watch by Agat"),
            _listing("Fossil Grant Chronograph Brown Leather"),
            _listing("Glass Martini Shaker"),
            _listing("Cabinet/Wine bar. Liquor cabinet."),
            _listing("Bar items ALL x $20"),
            _listing("Ninja thristi"),
            _listing("Breville Juice Fountain Juicer"),
        ]
        result = filter_by_relevance(listings, "espresso machine")
        titles = [l.title for l in result]
        assert "2 Pair Gloria Vanderbilt Capri Pants Size 18W" not in titles
        assert "Russian Stop Watch by Agat" not in titles
        assert "Fossil Grant Chronograph Brown Leather" not in titles
        assert "Glass Martini Shaker" not in titles
        assert "Ninja thristi" not in titles

    def test_espresso_color_false_positive_is_acceptable(self):
        """'Espresso Brown Entertainment center' passes because of 'espresso'.
        This is acceptable — the triage LLM will catch it."""
        listings = [_listing("Espresso Brown Entertainment center")]
        result = filter_by_relevance(listings, "espresso machine")
        # This passes — "espresso" matches. Triage LLM handles it.
        assert len(result) == 1


# ---------------------------------------------------------------------------
# filter_by_relevance — Pokemon Cards
# ---------------------------------------------------------------------------

class TestPokemonCardsRelevance:
    """Real data: "pokemon cards" returned sports cards, flash cards, etc."""

    def test_keeps_pokemon(self):
        listings = [
            _listing("24 Pokémon cards from 1999"),
            _listing("Tag teams, captain pikachu, and more pokemon singles"),
            _listing("Sports cards & Pokémon & Superman lot"),
            _listing("Used Pokemon Mega Construx Scyther Figure Set"),
        ]
        result = filter_by_relevance(listings, "pokemon cards")
        assert len(result) == 4

    def test_keeps_pokemon_adjacent_via_expansion(self):
        """Mewtwo, Soul Silver, Reshiram are related to 'pokemon'."""
        listings = [
            _listing("Mega Mewtwo"),
            _listing("Used Soul Silver DS game"),
            _listing("(6) Reshiram Celebrations Cards"),
        ]
        result = filter_by_relevance(listings, "pokemon cards")
        assert len(result) == 3

    def test_keeps_non_pokemon_cards_at_min1(self):
        """Non-pokemon cards pass at min=1 because "cards"/"card" match."""
        listings = [
            _listing("Vintage E.T. Cards"),
            _listing("SEALED NBA Utah JAZZ Playing Cards"),
            _listing("Vintage 1970's Charlie's Angel Cards"),
            _listing("Math flash cards and booklet"),
            _listing("Game of Thrones card game"),
        ]
        result = filter_by_relevance(listings, "pokemon cards")
        # These contain "cards/card" — they pass at min=1. That's intentional:
        # the filter is a LIGHT sanity check, triage LLM decides the rest.
        assert len(result) >= 4

    def test_drops_no_card_items_at_all(self):
        """Items with zero card/pokemon overlap should always be dropped."""
        listings = [
            _listing("Packers jersey"),
            _listing("Ninja Creami"),
            _listing("Disney Elsa Doll"),
        ]
        result = filter_by_relevance(listings, "pokemon cards")
        assert len(result) == 0

    def test_drops_real_garbage_from_run(self):
        """From actual run: non-pokemon, non-card items should be dropped."""
        listings = [
            _listing("Packers"),
            _listing("Wayne Gretzky: 99 Stories of the Game (Hardcover)"),
            _listing("Kids Milwaukee Bucks shirt"),
            _listing("Disney Elsa Doll"),
            _listing("Ninja Creami"),
            _listing("Women's vest"),
        ]
        result = filter_by_relevance(listings, "pokemon cards")
        assert len(result) == 0


# ---------------------------------------------------------------------------
# filter_by_relevance — Stainless Steel Cookware
# ---------------------------------------------------------------------------

class TestStainlessSteelCookwareRelevance:
    """Real data: returned cast iron, Corning Ware, soup items, etc."""

    def test_keeps_relevant(self):
        listings = [
            _listing("Vintage Stainless Flatware in Box"),
            _listing("Stainless Steel Pizza Oven"),
        ]
        result = filter_by_relevance(listings, "stainless steel cookware")
        assert len(result) == 2

    def test_keeps_cookware_adjacent_via_expansion(self):
        """Pots, pans, skillets are related to 'cookware'."""
        listings = [
            _listing("Cast iron pans"),  # "pans" is related to "cookware"
            _listing("Le Creuset Red Cast Iron Skillet"),  # "skillet" is related
            _listing("West Bend stock pot"),  # contains "pot", related via stockpot
        ]
        result = filter_by_relevance(listings, "stainless steel cookware")
        # "pans" and "skillet" are in _RELATED_TERMS for "cookware"
        assert len(result) >= 2

    def test_drops_garbage(self):
        listings = [
            _listing("Oven pads"),
            _listing("campbell Soup items"),
            _listing("Pfaltzgraff Tea Rose Collection, Bunny Casserole"),
            _listing("Vintage 60s Ekco Flint Harvest Wheat Spoon Ladle Fork Set"),
        ]
        result = filter_by_relevance(listings, "stainless steel cookware")
        assert len(result) == 0


# ---------------------------------------------------------------------------
# filter_by_relevance — Over The Toilet Shelves
# ---------------------------------------------------------------------------

class TestOverToiletShelvesRelevance:
    """Real data: returned shower curtains, laundry baskets, soap dishes."""

    def test_drops_obvious_garbage(self):
        listings = [
            _listing("Vintage Ceramic Floral Soap Dish"),
            _listing("Leather Office Chair"),
            _listing("Autographed Jordan Hicks Jersey"),
            _listing("Pumpkin costume"),
            _listing("Orthopedic pillow top dog bed"),
        ]
        result = filter_by_relevance(listings, "over the toilet shelves")
        assert len(result) == 0

    def test_keeps_bathroom_adjacent_via_expansion(self):
        """'toilet' expands to 'bathroom'; 'shelves' expands to 'shelf/rack/organizer'."""
        listings = [
            _listing("Bathroom Above the Toilet Shelf"),
            _listing("Over Toilet Storage Shelves - White"),
            _listing("Bathroom shelf organizer"),  # "bathroom" + "shelf" from expansion
        ]
        result = filter_by_relevance(listings, "over the toilet shelves")
        assert len(result) == 3

    def test_shelf_related_passes(self):
        """'shelves' related terms include 'shelf', 'shelving', 'rack', 'organizer'."""
        listings = [
            _listing("Corner solid oak shelf"),  # "shelf" is related to "shelves"
            _listing("Misc shelf/stands"),
        ]
        result = filter_by_relevance(listings, "over the toilet shelves")
        assert len(result) == 2

    def test_still_drops_non_shelf_bathroom_items(self):
        """Shower curtains etc. should still be dropped."""
        listings = [
            _listing("Shower Curtain Rings"),
            _listing("Barbie shower curtain"),
            _listing("New Better Homes Shower Curtain Hooks Set"),
        ]
        result = filter_by_relevance(listings, "over the toilet shelves")
        assert len(result) == 0


# ---------------------------------------------------------------------------
# filter_by_relevance — Legos
# ---------------------------------------------------------------------------

class TestLegosRelevance:
    """Legos search was returning kept=0 — no related terms defined."""

    def test_keeps_lego_items(self):
        listings = [
            _listing("LEGO Star Wars Set 75192"),
            _listing("Legos mixed lot 5 lbs"),
            _listing("LEGO Ninjago Dragon"),
            _listing("Duplo Farm Set"),
            _listing("Bionicle figure lot"),
        ]
        result = filter_by_relevance(listings, "Legos")
        assert len(result) == 5

    def test_drops_non_lego(self):
        listings = [
            _listing("Nike Air Max shoes"),
            _listing("Wooden blocks toddler toy"),
            _listing("Hot Wheels car lot"),
        ]
        result = filter_by_relevance(listings, "Legos")
        assert len(result) == 0


# ---------------------------------------------------------------------------
# filter_by_relevance — Glassware
# ---------------------------------------------------------------------------

class TestGlasswareRelevance:
    """Glassware search was returning kept=0 — no related terms defined."""

    def test_keeps_glassware_items(self):
        listings = [
            _listing("Crystal Wine Glasses Set of 4"),
            _listing("Vintage Stemware Collection"),
            _listing("Glass Pitcher with lid"),
            _listing("Martini Glasses"),
            _listing("Crystal Decanter"),
        ]
        result = filter_by_relevance(listings, "glassware")
        assert len(result) == 5

    def test_drops_non_glassware(self):
        listings = [
            _listing("Ceramic Dinner Plates"),
            _listing("Plastic Storage Bins"),
            _listing("Wooden Cutting Board"),
        ]
        result = filter_by_relevance(listings, "glassware")
        assert len(result) == 0


# ---------------------------------------------------------------------------
# filter_by_relevance — Cookies
# ---------------------------------------------------------------------------

class TestCookiesRelevance:
    """Cookies search was returning kept=0 — no related terms defined."""

    def test_keeps_cookie_items(self):
        listings = [
            _listing("Cookie Jar Vintage Ceramic"),
            _listing("Cookie Cutters Christmas Set"),
            _listing("Baking Sheet Set"),
            _listing("Sugar Cookie Mix lot"),
        ]
        result = filter_by_relevance(listings, "cookies")
        assert len(result) == 4

    def test_drops_non_cookie(self):
        listings = [
            _listing("Cast iron skillet"),
            _listing("Vintage lamp"),
        ]
        result = filter_by_relevance(listings, "cookies")
        assert len(result) == 0


# ---------------------------------------------------------------------------
# Typo tolerance
# ---------------------------------------------------------------------------

class TestTypoTolerance:
    """Typo corrections: 'cookeware' should activate 'cookware' expansion."""

    def test_cookeware_typo_expands(self):
        listings = [
            _listing("Stainless Steel Pizza Oven"),
            _listing("Cast iron pans"),
            _listing("Le Creuset Skillet"),
            _listing("campbell Soup items"),
        ]
        # "cookeware" is a typo for "cookware" — should still expand
        result = filter_by_relevance(listings, "stainless steel cookeware")
        titles = [l.title for l in result]
        assert "Stainless Steel Pizza Oven" in titles
        assert "Cast iron pans" in titles  # "pans" is related to cookware
        assert "Le Creuset Skillet" in titles  # "skillet" is related
        assert "campbell Soup items" not in titles

    def test_expresso_typo_expands(self):
        listings = [_listing("Coffee Maker"), _listing("Random shoes")]
        result = filter_by_relevance(listings, "expresso machine")
        assert len(result) == 1
        assert result[0].title == "Coffee Maker"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestRelevanceEdgeCases:
    def test_empty_listings(self):
        assert filter_by_relevance([], "smart tv") == []

    def test_empty_query(self):
        listings = [_listing("Anything")]
        assert filter_by_relevance(listings, "") == listings

    def test_single_word_query(self):
        listings = [
            _listing("PS5 Console"),
            _listing("Random desk lamp"),
        ]
        result = filter_by_relevance(listings, "PS5")
        assert len(result) == 1
        assert result[0].title == "PS5 Console"

    def test_case_insensitive(self):
        listings = [_listing("KITCHENAID MIXER")]
        result = filter_by_relevance(listings, "kitchenaid")
        assert len(result) == 1

    def test_compound_form(self):
        """'kitchen aid' should match 'KitchenAid' via compound join."""
        listings = [_listing("KitchenAid Professional 600")]
        result = filter_by_relevance(listings, "kitchen aid")
        assert len(result) == 1
