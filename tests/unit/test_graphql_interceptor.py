"""Unit tests for the GraphQL network interceptor."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from poob.browser.graphql_interceptor import (
    GraphQLInterceptor,
    GraphQLListingData,
    parse_graphql_listings,
)


# ---------------------------------------------------------------------------
# Sample GraphQL payloads
# ---------------------------------------------------------------------------
def _make_feed_edge(ext_id: str, title: str, price: float, **extra) -> dict:
    """Build a minimal GraphQL feed edge node matching FB's shape."""
    node = {
        "listing": {
            "id": ext_id,
            "marketplace_listing_title": title,
            "listing_price": {
                "amount": str(price),
                "formatted_amount": f"${price:,.0f}",
                "currency": "USD",
            },
            "primary_listing_photo": {
                "image": {"uri": f"https://scontent.fbcdn.net/{ext_id}.jpg"}
            },
            "location": {
                "reverse_geocode": {
                    "city_page": {"display_name": "Appleton, WI"}
                }
            },
            "creation_time": 1700000000,
            "marketplace_listing_condition_type": "USED_GOOD",
            **extra,
        }
    }
    return {"node": node}


def _make_feed_response(*edges) -> dict:
    """Wrap edges in a full GraphQL response structure."""
    return {
        "data": {
            "marketplace_search": {
                "feed_units": {
                    "edges": list(edges),
                }
            }
        }
    }


def _make_alt_response(*nodes) -> dict:
    """Wrap listing nodes in an alternate GraphQL structure."""
    return {
        "data": {
            "viewer": {
                "marketplace_feed_stories": {
                    "edges": [{"node": n} for n in nodes]
                }
            }
        }
    }


# ---------------------------------------------------------------------------
# parse_graphql_listings tests
# ---------------------------------------------------------------------------
class TestParseGraphQLListings:
    """Tests for the static parser function."""

    def test_parses_standard_feed_response(self):
        """Parse a standard MarketplaceCategoryFeedQuery response."""
        body = json.dumps(_make_feed_response(
            _make_feed_edge("001", "PS5 Disc Edition", 250.0),
            _make_feed_edge("002", "Mountain Bike", 150.0),
        ))
        results = parse_graphql_listings(body)
        assert len(results) == 2
        assert results[0].external_id == "001"
        assert results[0].title == "PS5 Disc Edition"
        assert results[0].price == 250.0
        assert results[0].location == "Appleton, WI"
        assert results[1].external_id == "002"

    def test_extracts_image_urls(self):
        """Image URLs should be extracted from primary_listing_photo."""
        body = json.dumps(_make_feed_response(
            _make_feed_edge("001", "PS5", 250.0),
        ))
        results = parse_graphql_listings(body)
        assert results[0].image_urls == ["https://scontent.fbcdn.net/001.jpg"]

    def test_extracts_condition(self):
        """Condition should be extracted from marketplace_listing_condition_type."""
        body = json.dumps(_make_feed_response(
            _make_feed_edge("001", "PS5", 250.0),
        ))
        results = parse_graphql_listings(body)
        assert results[0].condition == "USED_GOOD"

    def test_handles_missing_price(self):
        """Listings without a price should parse with price=None."""
        edge = _make_feed_edge("001", "Free Couch", 0.0)
        edge["node"]["listing"]["listing_price"] = None
        body = json.dumps(_make_feed_response(edge))
        results = parse_graphql_listings(body)
        assert len(results) == 1
        assert results[0].price is None

    def test_handles_malformed_json(self):
        """Malformed JSON should return empty list, not crash."""
        results = parse_graphql_listings("{invalid json!!!")
        assert results == []

    def test_handles_empty_edges(self):
        """Response with no edges should return empty list."""
        body = json.dumps(_make_feed_response())
        results = parse_graphql_listings(body)
        assert results == []

    def test_handles_empty_body(self):
        """Empty string body should return empty list."""
        results = parse_graphql_listings("")
        assert results == []

    def test_fallback_recursive_search(self):
        """When known paths fail, recursive search finds listings by id+listing_price pattern."""
        # Non-standard structure with listing data nested oddly
        weird_structure = {
            "data": {
                "something_unexpected": {
                    "items": [
                        {
                            "id": "999",
                            "marketplace_listing_title": "Weird Listing",
                            "listing_price": {"amount": "75", "currency": "USD"},
                            "primary_listing_photo": {"image": {"uri": "https://img.jpg"}},
                        }
                    ]
                }
            }
        }
        body = json.dumps(weird_structure)
        results = parse_graphql_listings(body)
        assert len(results) == 1
        assert results[0].external_id == "999"
        assert results[0].title == "Weird Listing"
        assert results[0].price == 75.0

    def test_deduplicates_by_external_id(self):
        """Same listing appearing in multiple places should only appear once."""
        body = json.dumps(_make_feed_response(
            _make_feed_edge("001", "PS5", 250.0),
            _make_feed_edge("001", "PS5", 250.0),  # duplicate
            _make_feed_edge("002", "Bike", 100.0),
        ))
        results = parse_graphql_listings(body)
        assert len(results) == 2

    def test_builds_listing_url(self):
        """Listing URL should be constructed from external_id."""
        body = json.dumps(_make_feed_response(
            _make_feed_edge("12345", "Item", 50.0),
        ))
        results = parse_graphql_listings(body)
        assert "12345" in results[0].listing_url

    def test_cents_conversion_amount_with_offset_in_currency(self):
        """amount_with_offset_in_currency is CENTS — must divide by 100."""
        edge = _make_feed_edge("001", "Table", 0.0)
        edge["node"]["listing"]["listing_price"] = {
            "amount_with_offset_in_currency": "1850",
            "currency": "USD",
        }
        body = json.dumps(_make_feed_response(edge))
        results = parse_graphql_listings(body)
        assert len(results) == 1
        assert results[0].price == 18.50

    def test_cents_conversion_amount_with_offset_amount(self):
        """amount_with_offset_amount is CENTS — must divide by 100."""
        edge = _make_feed_edge("002", "Chair", 0.0)
        edge["node"]["listing"]["listing_price"] = {
            "amount_with_offset_amount": "5000",
            "currency": "USD",
        }
        body = json.dumps(_make_feed_response(edge))
        results = parse_graphql_listings(body)
        assert len(results) == 1
        assert results[0].price == 50.0

    def test_cents_conversion_amount_with_offset(self):
        """amount_with_offset is CENTS — must divide by 100."""
        edge = _make_feed_edge("003", "Lamp", 0.0)
        edge["node"]["listing"]["listing_price"] = {
            "amount_with_offset": "2599",
            "currency": "USD",
        }
        body = json.dumps(_make_feed_response(edge))
        results = parse_graphql_listings(body)
        assert len(results) == 1
        assert results[0].price == 25.99

    def test_amount_dollars_preferred_over_cents(self):
        """When both 'amount' (dollars) and offset (cents) present, use dollars."""
        edge = _make_feed_edge("004", "Both", 0.0)
        edge["node"]["listing"]["listing_price"] = {
            "amount": "18.50",
            "amount_with_offset_in_currency": "1850",
            "currency": "USD",
        }
        body = json.dumps(_make_feed_response(edge))
        results = parse_graphql_listings(body)
        assert results[0].price == 18.50

    def test_formatted_amount_fallback(self):
        """formatted_amount should be used when amount and offset are missing."""
        edge = _make_feed_edge("005", "Widget", 0.0)
        edge["node"]["listing"]["listing_price"] = {
            "formatted_amount": "$42.00",
            "currency": "USD",
        }
        body = json.dumps(_make_feed_response(edge))
        results = parse_graphql_listings(body)
        assert results[0].price == 42.0


# ---------------------------------------------------------------------------
# GraphQLInterceptor tests
# ---------------------------------------------------------------------------
class TestGraphQLInterceptor:
    """Tests for the interceptor lifecycle."""

    def test_drain_returns_and_clears(self):
        """drain() should return captured data and clear the buffer."""
        interceptor = GraphQLInterceptor()
        interceptor._captured.append(
            GraphQLListingData(external_id="001", title="Test", price=10.0)
        )
        interceptor._captured.append(
            GraphQLListingData(external_id="002", title="Test2", price=20.0)
        )

        results = interceptor.drain()
        assert len(results) == 2

        # Second drain should be empty
        results2 = interceptor.drain()
        assert len(results2) == 0

    def test_drain_empty_when_nothing_captured(self):
        """drain() on fresh interceptor should return empty list."""
        interceptor = GraphQLInterceptor()
        assert interceptor.drain() == []

    def test_on_request_tracks_graphql_posts(self):
        """_on_request should track POST requests to /api/graphql/."""
        interceptor = GraphQLInterceptor()
        params = {
            "requestId": "req_1",
            "request": {
                "url": "https://www.facebook.com/api/graphql/",
                "method": "POST",
                "postData": '{"doc_id":"MarketplaceCategoryFeedQuery"}',
            },
        }
        interceptor._on_request(params, None)
        assert "req_1" in interceptor._pending_requests

    def test_on_request_ignores_non_graphql(self):
        """_on_request should ignore non-graphql URLs."""
        interceptor = GraphQLInterceptor()
        params = {
            "requestId": "req_1",
            "request": {
                "url": "https://www.facebook.com/marketplace/category/electronics",
                "method": "GET",
            },
        }
        interceptor._on_request(params, None)
        assert "req_1" not in interceptor._pending_requests

    def test_process_response_body_parses_listings(self):
        """_process_response_body should parse GraphQL JSON and add to captured list."""
        interceptor = GraphQLInterceptor()
        body = json.dumps(_make_feed_response(
            _make_feed_edge("001", "PS5", 250.0),
        ))
        interceptor._process_response_body(body)
        assert len(interceptor._captured) == 1
        assert interceptor._captured[0].external_id == "001"

    def test_process_response_body_deduplicates(self):
        """Already-captured IDs should not be added again."""
        interceptor = GraphQLInterceptor()
        interceptor._seen_ids.add("001")

        body = json.dumps(_make_feed_response(
            _make_feed_edge("001", "PS5", 250.0),
            _make_feed_edge("002", "Bike", 100.0),
        ))
        interceptor._process_response_body(body)
        assert len(interceptor._captured) == 1
        assert interceptor._captured[0].external_id == "002"
