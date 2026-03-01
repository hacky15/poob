"""Tests for stealth profile and anti-detection measures."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from agentic_scraper.browser.stealth_profile import (
    STEALTH_SCRIPTS,
    apply_stealth_scripts,
    random_viewport,
)
from agentic_scraper.browser.stealth import simulate_mouse_movement


class TestRandomViewport:
    """Tests for viewport randomization."""

    def test_returns_valid_dimensions(self):
        """Viewport should have reasonable width and height."""
        vp = random_viewport()
        assert "width" in vp
        assert "height" in vp
        assert 700 <= vp["width"] <= 1940
        assert 700 <= vp["height"] <= 1100

    def test_jitter_varies(self):
        """Multiple calls should produce different sizes."""
        viewports = [random_viewport() for _ in range(50)]
        widths = {vp["width"] for vp in viewports}
        # With 50 samples across 6 base sizes + jitter, we should see variation
        assert len(widths) > 3

    def test_dimensions_are_integers(self):
        """Width and height should be integers."""
        vp = random_viewport()
        assert isinstance(vp["width"], int)
        assert isinstance(vp["height"], int)


class TestStealthScripts:
    """Tests for stealth JavaScript injection."""

    def test_scripts_are_non_empty(self):
        """Stealth scripts list should not be empty."""
        assert len(STEALTH_SCRIPTS) >= 4

    def test_scripts_are_strings(self):
        """All scripts should be strings."""
        for script in STEALTH_SCRIPTS:
            assert isinstance(script, str)
            assert len(script) > 10

    async def test_apply_stealth_scripts_calls_evaluate(self):
        """apply_stealth_scripts should call page.evaluate for each script."""
        page = AsyncMock()
        page.evaluate = AsyncMock(return_value=None)

        await apply_stealth_scripts(page)
        assert page.evaluate.call_count == len(STEALTH_SCRIPTS)

    async def test_apply_stealth_scripts_continues_on_failure(self):
        """If one script fails, others should still be applied."""
        page = AsyncMock()
        # First call succeeds, second raises, rest succeed
        page.evaluate = AsyncMock(
            side_effect=[None, Exception("Script error")] + [None] * 10
        )

        await apply_stealth_scripts(page)
        # Should have attempted all scripts
        assert page.evaluate.call_count == len(STEALTH_SCRIPTS)


class TestMouseMovement:
    """Tests for mouse movement simulation."""

    async def test_simulate_mouse_movement_calls_evaluate(self):
        """simulate_mouse_movement should dispatch mouse events via JS."""
        page = AsyncMock()
        page.evaluate = AsyncMock(return_value=None)

        await simulate_mouse_movement(page, movements=3)
        assert page.evaluate.call_count == 3

    async def test_simulate_mouse_movement_handles_errors(self):
        """Mouse simulation should not crash on errors."""
        page = AsyncMock()
        page.evaluate = AsyncMock(side_effect=Exception("Page error"))

        # Should not raise
        await simulate_mouse_movement(page, movements=2)
