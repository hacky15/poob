"""Tests for browser stealth utilities."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest


class TestRandomDelay:
    """Tests for random_delay()."""

    async def test_delay_within_bounds(self):
        """Delay value should be between min_ms and max_ms."""
        from poob.browser.stealth import random_delay

        with patch("poob.browser.stealth.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await random_delay(100, 200)
            mock_sleep.assert_called_once()
            actual_seconds = mock_sleep.call_args[0][0]
            assert 0.1 <= actual_seconds <= 0.2

    async def test_delay_calls_asyncio_sleep(self):
        """Should actually call asyncio.sleep."""
        from poob.browser.stealth import random_delay

        with patch("poob.browser.stealth.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await random_delay(50, 100)
            mock_sleep.assert_awaited_once()

    async def test_delay_with_equal_bounds(self):
        """When min == max, should sleep for exactly that duration."""
        from poob.browser.stealth import random_delay

        with patch("poob.browser.stealth.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await random_delay(500, 500)
            actual_seconds = mock_sleep.call_args[0][0]
            assert actual_seconds == pytest.approx(0.5, abs=0.001)

    async def test_delay_converts_ms_to_seconds(self):
        """ms arguments should be converted to seconds for asyncio.sleep."""
        from poob.browser.stealth import random_delay

        with patch("poob.browser.stealth.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await random_delay(1000, 1000)
            actual_seconds = mock_sleep.call_args[0][0]
            assert actual_seconds == pytest.approx(1.0, abs=0.001)


class TestRandomScrollPattern:
    """Tests for random_scroll_pattern()."""

    def test_returns_list_of_tuples(self):
        """Should return a list of (direction, pixels, pause_ms) tuples."""
        from poob.browser.stealth import random_scroll_pattern

        pattern = random_scroll_pattern()
        assert isinstance(pattern, list)
        assert len(pattern) > 0
        for item in pattern:
            assert len(item) == 3
            direction, pixels, pause_ms = item
            assert direction in ("up", "down")
            assert isinstance(pixels, int)
            assert pixels > 0
            assert isinstance(pause_ms, int)
            assert pause_ms > 0

    def test_pattern_has_varied_values(self):
        """Scroll amounts should not all be identical (randomized)."""
        from poob.browser.stealth import random_scroll_pattern

        pattern = random_scroll_pattern(steps=5)
        pixels_list = [p[1] for p in pattern]
        # With 5 random values, extremely unlikely they're all identical
        assert len(set(pixels_list)) > 1 or len(pattern) == 1

    def test_pattern_mostly_scrolls_down(self):
        """Most scrolls should be downward for browsing behavior."""
        from poob.browser.stealth import random_scroll_pattern

        pattern = random_scroll_pattern(steps=10)
        down_count = sum(1 for d, _, _ in pattern if d == "down")
        assert down_count >= len(pattern) // 2


class TestAddJitter:
    """Tests for add_jitter()."""

    def test_jitter_within_default_bounds(self):
        """Result should be within +/- 15% of target by default."""
        from poob.browser.stealth import add_jitter

        target = 10.0
        for _ in range(100):
            result = add_jitter(target)
            assert 8.5 <= result <= 11.5  # 10 +/- 15%

    def test_jitter_with_zero_pct(self):
        """With 0% jitter, should return exact value."""
        from poob.browser.stealth import add_jitter

        assert add_jitter(10.0, pct=0.0) == 10.0

    def test_jitter_with_custom_pct(self):
        """Custom percentage should change the bounds."""
        from poob.browser.stealth import add_jitter

        target = 100.0
        for _ in range(100):
            result = add_jitter(target, pct=0.5)
            assert 50.0 <= result <= 150.0  # 100 +/- 50%
