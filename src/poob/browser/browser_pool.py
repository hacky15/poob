"""Browser session pool for concurrent, resilient scraping.

Manages multiple browser contexts with separate profiles, cookies,
and optional proxy bindings. Provides automatic rotation, cooldown
on suspected bans, and lifecycle management.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from poob.utils.logging import get_logger

log = get_logger("browser.browser_pool")


@dataclass
class BrowserSlot:
    """A single browser context slot in the pool.

    Attributes:
        slot_id: Unique identifier for this slot.
        profile_dir: Persistent profile directory for cookies/state.
        session: The browser-use BrowserSession instance.
        page: Current page instance.
        is_healthy: Whether this slot is usable.
        cooldown_until: Timestamp when cooldown expires (0 = not cooling).
        consecutive_empty: Count of consecutive empty result sweeps.
        last_used: Timestamp of last usage.
        total_requests: Total requests served by this slot.
    """

    slot_id: int = 0
    profile_dir: str = ""
    session: Any = None
    page: Any = None
    is_healthy: bool = True
    cooldown_until: float = 0.0
    consecutive_empty: int = 0
    last_used: float = 0.0
    total_requests: int = 0


class BrowserPool:
    """Pool of browser contexts for concurrent scraping.

    Pre-launches N browser sessions with separate profiles.
    Provides round-robin allocation with automatic cooldown
    for banned sessions.

    Args:
        pool_size: Number of browser contexts to maintain.
        profiles_base_dir: Base directory for browser profiles.
        headless: Run browsers without visible window.
        cooldown_seconds: How long to cool down a suspected-banned session.
    """

    def __init__(
        self,
        *,
        pool_size: int = 3,
        profiles_base_dir: Path = Path("browser_profiles"),
        headless: bool = False,
        cooldown_seconds: float = 3600.0,
    ) -> None:
        self._pool_size = pool_size
        self._profiles_base = profiles_base_dir
        self._headless = headless
        self._cooldown_seconds = cooldown_seconds
        self._slots: list[BrowserSlot] = []
        self._current_slot_index = 0
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        """Launch all browser sessions in the pool."""
        from browser_use import BrowserProfile, BrowserSession

        for i in range(self._pool_size):
            profile_dir = self._profiles_base / f"pool_slot_{i}"
            profile_dir.mkdir(parents=True, exist_ok=True)

            profile = BrowserProfile(
                headless=self._headless,
                user_data_dir=str(profile_dir),
                window_size={"width": 1280, "height": 1100},
            )

            session = BrowserSession(browser_profile=profile)
            await session.start()

            page = await session.get_current_page()

            slot = BrowserSlot(
                slot_id=i,
                profile_dir=str(profile_dir),
                session=session,
                page=page,
                is_healthy=True,
                last_used=time.monotonic(),
            )
            self._slots.append(slot)

        log.info("Browser pool started", pool_size=self._pool_size)

    async def stop(self) -> None:
        """Gracefully shut down all browser sessions."""
        for slot in self._slots:
            if slot.session:
                try:
                    await slot.session.stop()
                except Exception as exc:
                    log.warning(
                        "Failed to stop browser slot",
                        slot_id=slot.slot_id,
                        error=str(exc)[:100],
                    )
        self._slots.clear()
        log.info("Browser pool stopped")

    async def acquire(self) -> BrowserSlot:
        """Get the next available browser slot.

        Uses round-robin with cooldown awareness. Skips slots that
        are in cooldown or unhealthy.

        Returns:
            An available BrowserSlot.

        Raises:
            RuntimeError: If no healthy slots are available.
        """
        async with self._lock:
            now = time.monotonic()
            attempts = 0

            while attempts < self._pool_size:
                slot = self._slots[self._current_slot_index]
                self._current_slot_index = (
                    (self._current_slot_index + 1) % self._pool_size
                )

                if not slot.is_healthy:
                    attempts += 1
                    continue

                if now < slot.cooldown_until:
                    attempts += 1
                    continue

                slot.last_used = now
                slot.total_requests += 1

                # Refresh page reference if needed
                if slot.session and slot.page is None:
                    try:
                        slot.page = await slot.session.get_current_page()
                    except Exception:
                        slot.is_healthy = False
                        attempts += 1
                        continue

                return slot

            raise RuntimeError(
                "No healthy browser slots available in pool. "
                "All slots are either in cooldown or unhealthy."
            )

    def report_empty_sweep(self, slot: BrowserSlot) -> None:
        """Report that a sweep returned 0 results.

        After 3 consecutive empty sweeps, puts the slot in cooldown
        (suspected shadow ban).

        Args:
            slot: The slot that returned empty results.
        """
        slot.consecutive_empty += 1
        if slot.consecutive_empty >= 3:
            slot.cooldown_until = time.monotonic() + self._cooldown_seconds
            slot.consecutive_empty = 0
            log.warning(
                "Browser slot entering cooldown (suspected ban)",
                slot_id=slot.slot_id,
                cooldown_seconds=self._cooldown_seconds,
            )

    def report_success(self, slot: BrowserSlot) -> None:
        """Report a successful sweep (resets empty counter)."""
        slot.consecutive_empty = 0

    def get_stats(self) -> list[dict]:
        """Return status of all pool slots."""
        now = time.monotonic()
        return [
            {
                "slot_id": s.slot_id,
                "is_healthy": s.is_healthy,
                "in_cooldown": now < s.cooldown_until,
                "cooldown_remaining_s": max(0, s.cooldown_until - now),
                "consecutive_empty": s.consecutive_empty,
                "total_requests": s.total_requests,
            }
            for s in self._slots
        ]

    @property
    def healthy_count(self) -> int:
        """Number of healthy, non-cooldown slots."""
        now = time.monotonic()
        return sum(
            1 for s in self._slots
            if s.is_healthy and now >= s.cooldown_until
        )
