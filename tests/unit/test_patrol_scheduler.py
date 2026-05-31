"""Tests for PatrolScheduler - adaptive timing scheduler."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from poob.scanner.patrol_scheduler import (
    PatrolScheduler,
    _read_cgroup_memory_fraction,
)


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


class TestReliabilityWatchdog:
    """A wedged CDP get_page() does NOT honor asyncio.wait_for cancellation, so
    an in-cycle timeout cannot recover it — only restarting the process can.
    After N consecutive cycle failures (hang-abandon or error) the scheduler
    force-exits so the container restart policy recovers a fresh session,
    bounding worst-case zero-output time.
    See docs/incidents/main-browser-cdp-wedge-infinite-hang.md."""

    def test_non_int_config_falls_back_to_default(self, scheduler):
        # The default MagicMock config yields a non-int for the threshold; the
        # scheduler must fall back to the safe default (3), not crash.
        assert scheduler._max_consecutive_failures == 3

    def test_explicit_threshold_from_config(self, mock_engine):
        cfg = MagicMock(
            patrol_peak_interval_seconds=120, patrol_moderate_interval_seconds=300,
            patrol_offpeak_interval_seconds=600, patrol_dead_interval_seconds=900,
            patrol_peak_hours_start=16, patrol_peak_hours_end=21,
            display_timezone="America/Chicago", patrol_max_consecutive_failures=5,
        )
        sched = PatrolScheduler(engine=mock_engine, config=cfg)
        assert sched._max_consecutive_failures == 5

    def test_consecutive_failures_force_exit(self, scheduler):
        scheduler._max_consecutive_failures = 3
        with patch("poob.scanner.patrol_scheduler.os._exit") as mock_exit:
            scheduler._record_cycle_outcome(succeeded=False)
            scheduler._record_cycle_outcome(succeeded=False)
            mock_exit.assert_not_called()
            scheduler._record_cycle_outcome(succeeded=False)  # 3rd consecutive
            mock_exit.assert_called_once_with(1)

    def test_success_resets_failure_counter(self, scheduler):
        scheduler._max_consecutive_failures = 3
        with patch("poob.scanner.patrol_scheduler.os._exit") as mock_exit:
            scheduler._record_cycle_outcome(succeeded=False)
            scheduler._record_cycle_outcome(succeeded=False)
            scheduler._record_cycle_outcome(succeeded=True)  # reset
            scheduler._record_cycle_outcome(succeeded=False)
            scheduler._record_cycle_outcome(succeeded=False)
            mock_exit.assert_not_called()  # only 2 consecutive since reset
            scheduler._record_cycle_outcome(succeeded=False)  # now 3rd
            mock_exit.assert_called_once_with(1)


class TestMemoryPressureGuard:
    """Chromium leaks renderer processes over hours; rather than risk reaping
    the live browser, the scheduler force-exits when container memory nears the
    cgroup cap so a fresh process reclaims it. See
    docs/incidents/main-browser-cdp-wedge-infinite-hang.md."""

    def test_cgroup_v2_fraction(self, tmp_path):
        (tmp_path / "memory.max").write_text("4000000000\n")
        (tmp_path / "memory.current").write_text("3400000000\n")
        frac = _read_cgroup_memory_fraction(root=str(tmp_path))
        assert frac == pytest.approx(0.85, abs=1e-6)

    def test_cgroup_v2_unlimited_returns_none(self, tmp_path):
        (tmp_path / "memory.max").write_text("max\n")
        (tmp_path / "memory.current").write_text("3400000000\n")
        # No v1 files either -> None (guard skipped).
        assert _read_cgroup_memory_fraction(root=str(tmp_path)) is None

    def test_cgroup_v1_fraction(self, tmp_path):
        v1 = tmp_path / "memory"
        v1.mkdir()
        (v1 / "memory.limit_in_bytes").write_text("2000000000\n")
        (v1 / "memory.usage_in_bytes").write_text("1800000000\n")
        frac = _read_cgroup_memory_fraction(root=str(tmp_path))
        assert frac == pytest.approx(0.9, abs=1e-6)

    def test_missing_files_returns_none(self, tmp_path):
        assert _read_cgroup_memory_fraction(root=str(tmp_path)) is None

    def test_restarts_above_threshold(self, scheduler):
        scheduler._memory_restart_pct = 0.85
        scheduler._memory_restart_min_uptime_s = 0.0  # past the warmup
        with patch(
            "poob.scanner.patrol_scheduler._read_cgroup_memory_fraction",
            return_value=0.92,
        ), patch("poob.scanner.patrol_scheduler.os._exit") as mock_exit:
            scheduler._maybe_restart_on_memory_pressure()
            mock_exit.assert_called_once_with(1)

    def test_no_restart_below_threshold(self, scheduler):
        scheduler._memory_restart_pct = 0.85
        scheduler._memory_restart_min_uptime_s = 0.0
        with patch(
            "poob.scanner.patrol_scheduler._read_cgroup_memory_fraction",
            return_value=0.70,
        ), patch("poob.scanner.patrol_scheduler.os._exit") as mock_exit:
            scheduler._maybe_restart_on_memory_pressure()
            mock_exit.assert_not_called()

    def test_no_restart_when_cgroup_unavailable(self, scheduler):
        scheduler._memory_restart_pct = 0.85
        scheduler._memory_restart_min_uptime_s = 0.0
        with patch(
            "poob.scanner.patrol_scheduler._read_cgroup_memory_fraction",
            return_value=None,
        ), patch("poob.scanner.patrol_scheduler.os._exit") as mock_exit:
            scheduler._maybe_restart_on_memory_pressure()
            mock_exit.assert_not_called()

    def test_no_restart_within_warmup(self, scheduler):
        scheduler._memory_restart_pct = 0.85
        scheduler._memory_restart_min_uptime_s = 600.0
        scheduler._started_monotonic = time.monotonic()  # just started
        with patch(
            "poob.scanner.patrol_scheduler._read_cgroup_memory_fraction",
            return_value=0.99,
        ), patch("poob.scanner.patrol_scheduler.os._exit") as mock_exit:
            scheduler._maybe_restart_on_memory_pressure()
            mock_exit.assert_not_called()

    def test_disabled_when_pct_zero(self, scheduler):
        scheduler._memory_restart_pct = 0.0
        scheduler._memory_restart_min_uptime_s = 0.0
        with patch(
            "poob.scanner.patrol_scheduler._read_cgroup_memory_fraction",
            return_value=0.99,
        ) as mock_read, patch("poob.scanner.patrol_scheduler.os._exit") as mock_exit:
            scheduler._maybe_restart_on_memory_pressure()
            mock_exit.assert_not_called()
            mock_read.assert_not_called()  # short-circuits before reading
