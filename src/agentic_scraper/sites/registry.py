"""Site adapter registry with auto-discovery."""

from __future__ import annotations

from importlib import import_module
from pathlib import Path

from agentic_scraper.sites.base import SiteAdapter
from agentic_scraper.utils.logging import get_logger

log = get_logger("sites.registry")


class SiteRegistry:
    """Discovers and manages site adapters from the sites/ directory.

    Walks subdirectories of sites/, imports any that export an `Adapter`
    attribute, and registers them by site_name. Zero configuration needed
    to add a new site - just create the directory with the right exports.
    """

    def __init__(self) -> None:
        self._adapters: dict[str, SiteAdapter] = {}

    def discover(self) -> None:
        """Walk sites/ subdirectories and register adapters.

        Each subdirectory must have an __init__.py that exports an `Adapter`
        class. The Adapter is instantiated and registered by its site_name.
        """
        sites_dir = Path(__file__).parent
        for child in sorted(sites_dir.iterdir()):
            if not child.is_dir():
                continue
            if child.name.startswith("__"):
                continue
            if not (child / "__init__.py").exists():
                continue

            try:
                module = import_module(f"agentic_scraper.sites.{child.name}")
                if not hasattr(module, "Adapter"):
                    log.debug("Skipping site module (no Adapter export)", module=child.name)
                    continue

                adapter = module.Adapter()
                self._adapters[adapter.site_name] = adapter
                log.info("Registered site adapter", site=adapter.site_name)

            except Exception as exc:
                log.error("Failed to load site adapter", module=child.name, error=str(exc))

    def get(self, site_name: str) -> SiteAdapter | None:
        """Get a registered adapter by site name.

        Args:
            site_name: The unique site identifier (e.g. 'facebook_marketplace').

        Returns:
            The adapter instance, or None if not registered.
        """
        return self._adapters.get(site_name)

    def list_sites(self) -> list[str]:
        """Return names of all registered site adapters."""
        return list(self._adapters.keys())
