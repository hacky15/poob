"""Tests for site adapter registry and auto-discovery."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


class TestSiteRegistry:
    """Tests for SiteRegistry auto-discovery."""

    def test_discover_finds_facebook_adapter(self):
        """Registry should discover the Facebook Marketplace adapter."""
        from poob.sites.registry import SiteRegistry

        registry = SiteRegistry()
        registry.discover()
        assert "facebook_marketplace" in registry.list_sites()

    def test_get_returns_adapter_by_name(self):
        """get() should return the correct adapter instance."""
        from poob.sites.registry import SiteRegistry

        registry = SiteRegistry()
        registry.discover()
        adapter = registry.get("facebook_marketplace")
        assert adapter is not None
        assert adapter.site_name == "facebook_marketplace"

    def test_get_returns_none_for_unknown(self):
        """get() should return None for an unregistered site."""
        from poob.sites.registry import SiteRegistry

        registry = SiteRegistry()
        registry.discover()
        assert registry.get("nonexistent_site") is None

    def test_list_sites_returns_all_names(self):
        """list_sites() should return names of all discovered adapters."""
        from poob.sites.registry import SiteRegistry

        registry = SiteRegistry()
        registry.discover()
        sites = registry.list_sites()
        assert isinstance(sites, list)
        assert len(sites) >= 1  # At least Facebook

    def test_registry_skips_non_adapter_directories(self):
        """Directories without an Adapter export should be skipped."""
        from poob.sites.registry import SiteRegistry

        registry = SiteRegistry()
        registry.discover()
        # __pycache__ and other non-adapter dirs should not appear
        for site in registry.list_sites():
            assert not site.startswith("__")

    def test_adapter_has_required_properties(self):
        """Discovered adapters should have the SiteAdapter protocol properties."""
        from poob.sites.registry import SiteRegistry

        registry = SiteRegistry()
        registry.discover()
        adapter = registry.get("facebook_marketplace")
        assert adapter is not None
        assert hasattr(adapter, "site_name")
        assert hasattr(adapter, "base_url")
        assert hasattr(adapter, "requires_login")
        assert hasattr(adapter, "scan")
        assert hasattr(adapter, "login")
