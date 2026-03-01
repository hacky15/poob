"""Tests for PatrolScanner - URL building, radius oscillation, category sweeping."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agentic_scraper.sites.facebook.patrol_scanner import (
    PatrolScanner,
    RadiusOscillator,
    build_patrol_url,
)
from agentic_scraper.storage.models import Listing


# --- build_patrol_url ---


class TestBuildPatrolUrl:
    def test_url_with_category(self):
        url = build_patrol_url("electronics", radius=20, days_since_listed=1)
        assert "facebook.com/marketplace/category/" in url
        assert "sortBy=creation_time_descend" in url
        assert "daysSinceListed=1" in url
        assert "radius=20" in url

    def test_url_without_category(self):
        url = build_patrol_url(None, radius=20, days_since_listed=1)
        assert "/category/" not in url
        assert "facebook.com/marketplace?" in url

    def test_url_includes_delivery_method(self):
        url = build_patrol_url("electronics")
        assert "deliveryMethod=local_pick_up" in url

    def test_url_includes_exact_false_by_default(self):
        url = build_patrol_url("electronics")
        assert "exact=false" in url

    def test_url_exact_true_omits_exact_param(self):
        url = build_patrol_url("electronics", exact=True)
        assert "exact=" not in url

    def test_url_custom_radius(self):
        url = build_patrol_url("electronics", radius=50)
        assert "radius=50" in url

    def test_url_custom_days_since_listed(self):
        url = build_patrol_url("electronics", days_since_listed=3)
        assert "daysSinceListed=3" in url

    def test_url_category_slug_mapping(self):
        """Known categories should map to Facebook slugs."""
        url = build_patrol_url("electronics")
        # Electronics maps to a slug in CATEGORY_SLUG_MAP
        assert "/marketplace/category/" in url

    def test_url_unknown_category_uses_raw_name(self):
        url = build_patrol_url("custom_category_xyz")
        assert "custom_category_xyz" in url

    def test_url_no_radius_omits_param(self):
        url = build_patrol_url("electronics", radius=0)
        assert "radius=" not in url


# --- RadiusOscillator ---


class TestRadiusOscillator:
    def test_default_cycle(self):
        osc = RadiusOscillator(base_radius=20, jitter=4)
        values = [osc.next() for _ in range(4)]
        assert values == [20, 22, 18, 24]

    def test_cycle_wraps_around(self):
        osc = RadiusOscillator(base_radius=20, jitter=4)
        values = [osc.next() for _ in range(8)]
        assert values == [20, 22, 18, 24, 20, 22, 18, 24]

    def test_custom_base_radius(self):
        osc = RadiusOscillator(base_radius=30, jitter=6)
        values = [osc.next() for _ in range(4)]
        assert values == [30, 33, 27, 36]

    def test_each_instance_has_own_state(self):
        osc1 = RadiusOscillator()
        osc2 = RadiusOscillator()
        v1 = osc1.next()
        v2 = osc2.next()
        assert v1 == v2  # Both start at same position


# --- PatrolScanner ---


class TestPatrolScanner:
    @pytest.fixture
    def mock_page(self):
        page = AsyncMock()
        page.evaluate = AsyncMock(return_value=[])
        return page

    @pytest.fixture
    def scanner(self):
        return PatrolScanner(scroll_steps=2, days_since_listed=1)

    @pytest.mark.asyncio
    async def test_sweep_returns_listings(self, scanner, mock_page):
        """sweep_category should return parsed Listing objects."""
        raw_data = [
            {
                "title": "PS5 Console",
                "price": 300,
                "location": "Appleton, WI",
                "listing_url": "https://facebook.com/marketplace/item/111",
                "external_id": "111",
                "image_url": "https://scontent.xx.fbcdn.net/img.jpg",
            }
        ]
        mock_page.evaluate = AsyncMock(return_value=raw_data)

        with patch(
            "agentic_scraper.sites.facebook.patrol_scanner.navigate_and_wait",
            new_callable=AsyncMock,
        ), patch(
            "agentic_scraper.sites.facebook.patrol_scanner.apply_scroll_pattern",
            new_callable=AsyncMock,
        ):
            listings = await scanner.sweep_category(mock_page, "electronics")

        assert len(listings) == 1
        assert listings[0].title == "PS5 Console"
        assert listings[0].external_id == "111"

    @pytest.mark.asyncio
    async def test_sweep_empty_results(self, scanner, mock_page):
        mock_page.evaluate = AsyncMock(return_value=[])

        with patch(
            "agentic_scraper.sites.facebook.patrol_scanner.navigate_and_wait",
            new_callable=AsyncMock,
        ), patch(
            "agentic_scraper.sites.facebook.patrol_scanner.apply_scroll_pattern",
            new_callable=AsyncMock,
        ):
            listings = await scanner.sweep_category(mock_page, "electronics")

        assert listings == []

    @pytest.mark.asyncio
    async def test_sweep_handles_string_response(self, scanner, mock_page):
        """page.evaluate sometimes returns JSON string instead of object."""
        raw_data = json.dumps([
            {
                "title": "Table",
                "price": 50,
                "location": "Appleton",
                "listing_url": "https://facebook.com/marketplace/item/222",
                "external_id": "222",
                "image_url": "",
            }
        ])
        mock_page.evaluate = AsyncMock(return_value=raw_data)

        with patch(
            "agentic_scraper.sites.facebook.patrol_scanner.navigate_and_wait",
            new_callable=AsyncMock,
        ), patch(
            "agentic_scraper.sites.facebook.patrol_scanner.apply_scroll_pattern",
            new_callable=AsyncMock,
        ):
            listings = await scanner.sweep_category(mock_page, "furniture")

        assert len(listings) == 1
        assert listings[0].title == "Table"

    @pytest.mark.asyncio
    async def test_sweep_handles_exception(self, scanner, mock_page):
        """Navigation failure should return empty list, not raise."""
        with patch(
            "agentic_scraper.sites.facebook.patrol_scanner.navigate_and_wait",
            new_callable=AsyncMock,
            side_effect=Exception("Navigation failed"),
        ):
            listings = await scanner.sweep_category(mock_page, "electronics")

        assert listings == []

    @pytest.mark.asyncio
    async def test_sweep_none_category(self, scanner, mock_page):
        """Passing None as category should sweep all listings."""
        mock_page.evaluate = AsyncMock(return_value=[])

        with patch(
            "agentic_scraper.sites.facebook.patrol_scanner.navigate_and_wait",
            new_callable=AsyncMock,
        ) as nav_mock, patch(
            "agentic_scraper.sites.facebook.patrol_scanner.apply_scroll_pattern",
            new_callable=AsyncMock,
        ):
            await scanner.sweep_category(mock_page, None)

        # URL should not contain /category/
        called_url = nav_mock.call_args[0][1]
        assert "/category/" not in called_url

    @pytest.mark.asyncio
    async def test_sweep_uses_oscillating_radius(self, mock_page):
        """Each sweep should use a different radius from the oscillator."""
        osc = RadiusOscillator(base_radius=20, jitter=4)
        scanner = PatrolScanner(radius_oscillator=osc, scroll_steps=1)
        mock_page.evaluate = AsyncMock(return_value=[])

        urls = []
        with patch(
            "agentic_scraper.sites.facebook.patrol_scanner.navigate_and_wait",
            new_callable=AsyncMock,
        ) as nav_mock, patch(
            "agentic_scraper.sites.facebook.patrol_scanner.apply_scroll_pattern",
            new_callable=AsyncMock,
        ):
            await scanner.sweep_category(mock_page, "electronics")
            urls.append(nav_mock.call_args[0][1])
            await scanner.sweep_category(mock_page, "electronics")
            urls.append(nav_mock.call_args[0][1])

        # Radius should differ between calls
        assert "radius=20" in urls[0]
        assert "radius=22" in urls[1]

    @pytest.mark.asyncio
    async def test_sweep_days_since_listed_override(self, scanner, mock_page):
        """days_since_listed parameter should override the default."""
        mock_page.evaluate = AsyncMock(return_value=[])

        with patch(
            "agentic_scraper.sites.facebook.patrol_scanner.navigate_and_wait",
            new_callable=AsyncMock,
        ) as nav_mock, patch(
            "agentic_scraper.sites.facebook.patrol_scanner.apply_scroll_pattern",
            new_callable=AsyncMock,
        ):
            await scanner.sweep_category(mock_page, "electronics", days_since_listed=3)

        called_url = nav_mock.call_args[0][1]
        assert "daysSinceListed=3" in called_url

    @pytest.mark.asyncio
    async def test_sweep_uses_fixed_radius(self, mock_page):
        """When fixed_radius is set, should use it instead of oscillator."""
        scanner = PatrolScanner(fixed_radius=40, scroll_steps=1)
        mock_page.evaluate = AsyncMock(return_value=[])

        with patch(
            "agentic_scraper.sites.facebook.patrol_scanner.navigate_and_wait",
            new_callable=AsyncMock,
        ) as nav_mock, patch(
            "agentic_scraper.sites.facebook.patrol_scanner.apply_scroll_pattern",
            new_callable=AsyncMock,
        ):
            await scanner.sweep_category(mock_page, "electronics")
            url1 = nav_mock.call_args[0][1]
            await scanner.sweep_category(mock_page, "furniture")
            url2 = nav_mock.call_args[0][1]

        # Both should use fixed radius, not oscillating
        assert "radius=40" in url1
        assert "radius=40" in url2
