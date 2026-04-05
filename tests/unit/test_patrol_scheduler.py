"""Tests for PatrolScheduler - adaptive timing scheduler."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agentic_scraper.scanner.patrol_scheduler import PatrolScheduler


@pytest.fixture
def mock_config():
    return MagicMock(
        patrol_peak_interval_seconds=120,
        patrol_moderate_interval_seconds=300,
        patrol_offpeak_interval_seconds=600,
        patrol_dead_interval_seconds=900,
        patrol_peak_hours_start=16,
        patrol_peak_hours_end=21,
        display_timezone="America/Chicago",
    )


@pytest.fixture
def mock_engine():
    engine = AsyncMock()
    engine.run_patrol_cycle = AsyncMock(return_value=MagicMock(
        categories_swept=3,
        new_listings=5,
        deals_found=1,
    ))
    return engine


@pytest.fixture
def scheduler(mock_engine, mock_config):
    return PatrolScheduler(engine=mock_engine, config=mock_config)


# --- Adaptive Interval ---


class TestCalculateInterval:
    def test_peak_hours_return_peak_interval(self, scheduler):
        """4 PM - 9 PM should use peak interval."""
        # Test at 5 PM (17:00)
        interval = scheduler._calculate_base_interval(17)
        assert interval == 120

    def test_moderate_hours(self, scheduler):
        """8 AM - 4 PM should use moderate interval."""
        interval = scheduler._calculate_base_interval(10)
        assert interval == 300

    def test_offpeak_hours_evening(self, scheduler):
        """9 PM - midnight should use offpeak interval."""
        interval = scheduler._calculate_base_interval(22)
        assert interval == 600

    def test_offpeak_hours_morning(self, scheduler):
        """6 AM - 8 AM should use offpeak interval."""
        interval = scheduler._calculate_base_interval(7)
        assert interval == 600

    def test_dead_hours(self, scheduler):
        """Midnight - 6 AM should use dead interval."""
        interval = scheduler._calculate_base_interval(3)
        assert interval == 900

    def test_boundary_peak_start(self, scheduler):
        """Exactly at peak start should be peak."""
        interval = scheduler._calculate_base_interval(16)
        assert interval == 120

    def test_boundary_peak_end(self, scheduler):
        """At peak end hour, should transition to offpeak."""
        interval = scheduler._calculate_base_interval(21)
        assert interval == 600


# --- Interface Compatibility ---


class TestSchedulerInterface:
    """PatrolScheduler must match ScanScheduler interface for ScanningCog."""

    def test_has_is_running(self, scheduler):
        assert hasattr(scheduler, "is_running")
        assert scheduler.is_running is False

    def test_has_is_paused(self, scheduler):
        assert hasattr(scheduler, "is_paused")
        assert scheduler.is_paused is False

    def test_has_last_scan_time(self, scheduler):
        assert hasattr(scheduler, "last_scan_time")
        assert scheduler.last_scan_time is None

    def test_has_next_scan_time(self, scheduler):
        assert hasattr(scheduler, "next_scan_time")
        assert scheduler.next_scan_time is None

    def test_pause_and_resume(self, scheduler):
        scheduler.pause()
        assert scheduler.is_paused is True
        scheduler.resume()
        assert scheduler.is_paused is False


# --- Start / Stop ---


class TestStartStop:
    @pytest.mark.asyncio
    async def test_start_sets_running(self, scheduler):
        await scheduler.start()
        assert scheduler.is_running is True
        await scheduler.stop()

    @pytest.mark.asyncio
    async def test_stop_clears_running(self, scheduler):
        await scheduler.start()
        await scheduler.stop()
        assert scheduler.is_running is False

    @pytest.mark.asyncio
    async def test_double_start_is_idempotent(self, scheduler):
        await scheduler.start()
        await scheduler.start()  # Should not raise or create duplicate tasks
        assert scheduler.is_running is True
        await scheduler.stop()

    @pytest.mark.asyncio
    async def test_stop_without_start(self, scheduler):
        """Stop on non-started scheduler should not raise."""
        await scheduler.stop()
        assert scheduler.is_running is False


# --- Trigger ---


class TestTriggerNow:
    @pytest.mark.asyncio
    async def test_trigger_now_runs_cycle(self, scheduler, mock_engine):
        """trigger_now should cause the engine to run a patrol cycle."""
        await scheduler.start()
        # Give the loop time to enter wait_for before triggering
        await asyncio.sleep(0.05)
        await scheduler.trigger_now()
        # Give the loop time to process the trigger and run the cycle
        await asyncio.sleep(0.15)
        await scheduler.stop()

        mock_engine.run_patrol_cycle.assert_called()


# --- Interval with Jitter ---


class TestIntervalJitter:
    def test_jittered_interval_is_within_bounds(self, scheduler):
        """Jitter should keep interval within ±40% of base."""
        base = 300
        # Run multiple times to check distribution
        for _ in range(100):
            jittered = scheduler._apply_jitter(base)
            assert base * 0.6 <= jittered <= base * 1.4


# --- Paused Behavior ---


class TestPausedBehavior:
    @pytest.mark.asyncio
    async def test_paused_skips_cycle(self, scheduler, mock_engine):
        """When paused, trigger_now should not run a cycle."""
        await scheduler.start()
        scheduler.pause()
        await scheduler.trigger_now()
        await asyncio.sleep(0.1)
        await scheduler.stop()

        # Engine should NOT have been called while paused
        # (the initial start doesn't immediately run either)
        mock_engine.run_patrol_cycle.assert_not_called()
