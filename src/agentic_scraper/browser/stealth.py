"""Human-like behavior utilities for browser automation stealth."""

from __future__ import annotations

import asyncio
import random


async def random_delay(min_ms: int, max_ms: int) -> None:
    """Sleep for a random duration between min_ms and max_ms milliseconds.

    Args:
        min_ms: Minimum delay in milliseconds.
        max_ms: Maximum delay in milliseconds.
    """
    seconds = random.uniform(min_ms / 1000, max_ms / 1000)
    await asyncio.sleep(seconds)


def random_scroll_pattern(steps: int = 5) -> list[tuple[str, int, int]]:
    """Generate a human-like scroll pattern.

    Returns a list of (direction, pixels, pause_ms) tuples that simulate
    natural browsing behavior - mostly scrolling down with occasional
    small upward corrections.

    Args:
        steps: Number of scroll actions in the pattern.

    Returns:
        List of (direction, pixels, pause_ms) tuples.
    """
    pattern: list[tuple[str, int, int]] = []
    for _ in range(steps):
        # 80% chance of scrolling down, 20% up (mimics real browsing)
        direction = "down" if random.random() < 0.8 else "up"
        pixels = random.randint(100, 600)
        pause_ms = random.randint(200, 1500)
        pattern.append((direction, pixels, pause_ms))
    return pattern


def add_jitter(seconds: float, pct: float = 0.15) -> float:
    """Add random jitter to a target duration.

    Args:
        seconds: Target duration in seconds.
        pct: Maximum jitter as a fraction (0.15 = +/- 15%).

    Returns:
        Duration with jitter applied.
    """
    if pct == 0.0:
        return seconds
    jitter = seconds * random.uniform(-pct, pct)
    return seconds + jitter
