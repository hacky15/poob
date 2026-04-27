"""PatrolScheduler - adaptive timing scheduler for patrol cycles.

Adjusts polling frequency based on time of day:
- Peak (4-9 PM):      120s (2 min) - highest FB activity
- Moderate (8 AM-4 PM): 300s (5 min)
- Off-peak (evening/early morning): 600s (10 min)
- Dead (midnight-6 AM):  900s (15 min)

Applies Gaussian jitter (±20%, clamped to ±40%) to avoid detection.
"""

from __future__ import annotations

import asyncio
import random
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from poob.utils.logging import get_logger

if TYPE_CHECKING:
    from poob.config import AppConfig
    from poob.scanner.patrol_engine import PatrolEngine

log = get_logger("scanner.patrol_scheduler")


class PatrolScheduler:
    """Manages periodic execution of patrol cycles with adaptive timing.

    Same public interface as ScanScheduler (for ScanningCog compatibility):
    start(), stop(), pause(), resume(), trigger_now(),
    is_running, is_paused, last_scan_time, next_scan_time.

    Args:
        engine: The PatrolEngine to drive.
        config: Application configuration with patrol timing fields.
    """

    def __init__(self, engine: PatrolEngine, config: AppConfig) -> None:
        self._engine = engine
        self._config = config
        self._is_running = False
        self._is_paused = False
        self._task: asyncio.Task | None = None
        self._trigger_event = asyncio.Event()
        self._last_scan_time: datetime | None = None
        self._next_scan_time: datetime | None = None

    @property
    def is_running(self) -> bool:
        """Whether the scheduler loop is active."""
        return self._is_running

    @property
    def is_paused(self) -> bool:
        """Whether patrolling is paused."""
        return self._is_paused

    @property
    def last_scan_time(self) -> datetime | None:
        """Timestamp of the last completed patrol cycle."""
        return self._last_scan_time

    @property
    def next_scan_time(self) -> datetime | None:
        """Estimated time of the next patrol cycle."""
        return self._next_scan_time

    async def start(self) -> None:
        """Start the patrol scheduler loop."""
        if self._is_running:
            return
        self._is_running = True
        self._task = asyncio.create_task(self._loop())
        log.info("Patrol scheduler started")

    async def stop(self) -> None:
        """Stop the patrol scheduler loop."""
        self._is_running = False
        if self._task:
            self._trigger_event.set()
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        log.info("Patrol scheduler stopped")

    def pause(self) -> None:
        """Pause patrolling (loop continues but skips cycles)."""
        self._is_paused = True
        log.info("Patrol scheduler paused")

    def resume(self) -> None:
        """Resume patrolling."""
        self._is_paused = False
        log.info("Patrol scheduler resumed")

    async def trigger_now(self) -> None:
        """Trigger an immediate patrol cycle, starting the scheduler if needed."""
        if not self._is_running:
            log.info("Scheduler not running — starting it now")
            await self.start()
        log.info("Immediate patrol triggered")
        self._trigger_event.set()

    def _calculate_base_interval(self, hour: int) -> int:
        """Calculate the base interval in seconds for the given hour.

        Args:
            hour: Hour of day (0-23).

        Returns:
            Base interval in seconds.
        """
        peak_start = self._config.patrol_peak_hours_start
        peak_end = self._config.patrol_peak_hours_end

        if peak_start <= hour < peak_end:
            return self._config.patrol_peak_interval_seconds
        if 8 <= hour < peak_start:
            return self._config.patrol_moderate_interval_seconds
        if 0 <= hour < 6:
            return self._config.patrol_dead_interval_seconds
        # Off-peak: 6-8 AM and peak_end+
        return self._config.patrol_offpeak_interval_seconds

    def _apply_jitter(self, base_seconds: int) -> float:
        """Apply Gaussian jitter to interval, clamped to ±40%.

        Args:
            base_seconds: The base interval in seconds.

        Returns:
            Jittered interval in seconds.
        """
        # Gaussian with std dev = 20% of base
        jitter = random.gauss(0, base_seconds * 0.2)
        # Clamp to ±40%
        max_jitter = base_seconds * 0.4
        jitter = max(-max_jitter, min(max_jitter, jitter))
        return base_seconds + jitter

    async def _loop(self) -> None:
        """Main scheduler loop. Waits for adaptive interval or trigger, then patrols."""
        while self._is_running:
            now = datetime.now(timezone.utc)
            local_tz = ZoneInfo(self._config.display_timezone)
            local_hour = now.astimezone(local_tz).hour
            base_interval = self._calculate_base_interval(local_hour)
            interval_seconds = self._apply_jitter(base_interval)

            # Check if trigger was already set (e.g., trigger_now() called before
            # the loop task started — race between create_task and event.set).
            if self._trigger_event.is_set():
                log.info("Patrol triggered manually (immediate)")
                self._trigger_event.clear()
            else:
                self._next_scan_time = now + timedelta(seconds=interval_seconds)
                log.info(
                    "Waiting for next patrol",
                    base_interval=base_interval,
                    jittered_interval=round(interval_seconds, 1),
                    next_at=self._next_scan_time.strftime("%H:%M:%S"),
                )

                try:
                    await asyncio.wait_for(
                        self._trigger_event.wait(),
                        timeout=interval_seconds,
                    )
                    log.info("Patrol triggered manually")
                except asyncio.TimeoutError:
                    log.info("Scheduled patrol starting")
                self._trigger_event.clear()

            if not self._is_running:
                break

            if self._is_paused:
                continue

            # Per-cycle hard timeout. Without this a single hung browser
            # call (browser-use's CDP get_page can block indefinitely when
            # the session degrades after a Discord reconnect) silently
            # kills the entire scheduler — observed in prod April 25:
            # one hung anonymous_browser.get_page() ate 51h of patrols.
            # 5 minutes is well above the typical full-cycle duration
            # (~10-15s anon-only, ~2-3 min with full enrichment).
            try:
                result = await asyncio.wait_for(
                    self._engine.run_patrol_cycle(),
                    timeout=300.0,
                )
                self._last_scan_time = datetime.now(timezone.utc)
                log.info(
                    "Patrol cycle finished",
                    new_listings=result.new_listings,
                    deals=result.deals_found,
                    duration=f"{result.duration_seconds:.1f}s",
                )
            except asyncio.TimeoutError:
                log.error(
                    "Patrol cycle hung past 300s — abandoned, "
                    "scheduler continues to next interval",
                )
            except Exception as exc:
                log.error("Patrol cycle error in scheduler", error=str(exc))
