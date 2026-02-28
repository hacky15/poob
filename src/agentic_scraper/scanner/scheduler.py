"""ScanScheduler - asyncio-based timing for periodic scan cycles."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from agentic_scraper.browser.stealth import add_jitter
from agentic_scraper.utils.logging import get_logger

if TYPE_CHECKING:
    from agentic_scraper.scanner.engine import ScanEngine

log = get_logger("scanner.scheduler")


class ScanScheduler:
    """Manages periodic execution of scan cycles with pause/resume support.

    Args:
        engine: The ScanEngine to drive.
        interval_minutes: Time between scans in minutes.
    """

    def __init__(self, engine: ScanEngine, interval_minutes: int = 15) -> None:
        self._engine = engine
        self._interval_minutes = interval_minutes
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
        """Whether scanning is paused."""
        return self._is_paused

    @property
    def last_scan_time(self) -> datetime | None:
        """Timestamp of the last completed scan."""
        return self._last_scan_time

    @property
    def next_scan_time(self) -> datetime | None:
        """Estimated time of the next scan."""
        return self._next_scan_time

    async def start(self) -> None:
        """Start the scheduler loop."""
        if self._is_running:
            return
        self._is_running = True
        self._task = asyncio.create_task(self._loop())
        log.info("Scan scheduler started", interval_minutes=self._interval_minutes)

    async def stop(self) -> None:
        """Stop the scheduler loop."""
        self._is_running = False
        if self._task:
            self._trigger_event.set()
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        log.info("Scan scheduler stopped")

    def pause(self) -> None:
        """Pause scanning (scheduler loop continues but skips scans)."""
        self._is_paused = True
        log.info("Scan scheduler paused")

    def resume(self) -> None:
        """Resume scanning."""
        self._is_paused = False
        log.info("Scan scheduler resumed")

    async def trigger_now(self) -> None:
        """Trigger an immediate scan cycle, bypassing the timer."""
        log.info("Immediate scan triggered")
        self._trigger_event.set()

    async def _loop(self) -> None:
        """Main scheduler loop. Waits for interval or trigger, then scans."""
        while self._is_running:
            interval_seconds = add_jitter(self._interval_minutes * 60)
            self._trigger_event.clear()

            self._next_scan_time = datetime.now(timezone.utc).replace(
                microsecond=0
            )
            # Add interval unless this is a triggered scan
            from datetime import timedelta

            self._next_scan_time += timedelta(seconds=interval_seconds)
            log.info(
                "Waiting for next scan",
                next_in_minutes=round(interval_seconds / 60, 1),
            )

            try:
                await asyncio.wait_for(
                    self._trigger_event.wait(),
                    timeout=interval_seconds,
                )
                log.info("Scan triggered manually")
            except asyncio.TimeoutError:
                log.info("Scheduled scan starting")

            if not self._is_running:
                break

            if self._is_paused:
                continue

            try:
                await self._engine.run_scan_cycle()
                self._last_scan_time = datetime.now(timezone.utc)
                log.info("Scan cycle finished, scheduling next")
            except Exception as exc:
                log.error("Scan cycle error in scheduler", error=str(exc))
